"""The `returns` table: reads (issue #50) and the write side of the returns API (issue #54).

The functions take a CONNECTION: the caller owns `async with pool.connection()`, so it also owns the
transaction and can run several statements in one (the status change, the decision row and the audit
row are written together). A connection is only ever held inside such a short block, never across an
LLM call. Nothing here commits.

The repository assumes the pool's default READ COMMITTED isolation. The key re-check after a
unique violation on the active-order-line index (is the colliding row the same key, so a replay, or
another one, so R4?) relies on a `23505` being raised once the colliding row is committed (or is one
of the caller's own), and on the next statement seeing that row. At REPEATABLE READ a row committed
after the snapshot would be invisible to that SELECT. The pool sets no isolation level and no
autocommit (app/db/pool.py).
"""

import math
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb

from app.db.audit import LLM_COLUMNS, insert_audit, llm_values
from app.db.models import (
    Applied,
    DecisionRecord,
    ExpiredKey,
    FinalizeResult,
    InFlight,
    Inserted,
    InsertResult,
    KeyConflict,
    KeyLookup,
    Lost,
    NoKey,
    PayloadMismatch,
    R4Denied,
    Replay,
    Retried,
    Retryable,
    RetryResult,
    ReturnFields,
    ReturnRow,
)
from app.db.vocabulary import (
    ERROR_TYPES,
    PERMANENT_ERROR_TYPES,
    TRANSIENT_ERROR_TYPES,
    failed_event,
)
from app.llm.types import LLMCallRecord
from app.policy import PolicyResult, RuleResult

# The columns of public.returns in table order, written out so that a test can compare three
# independent things: this list, the model's fields and the table in db/schema.sql.
RETURN_COLUMNS: tuple[str, ...] = (
    "id",
    "order_id",
    "order_line_id",
    "category",
    "amount_minor",
    "currency",
    "delivered_on",
    "reason",
    "status",
    "outcome",
    "error_type",
    "retry_after_seconds",
    "attempt",
    "as_of",
    "policy_version",
    "idempotency_key",
    "payload_hash",
    "key_generation",
    "created_at",
    "updated_at",
)
# An explicit column list, never SELECT *: a schema change then fails loudly instead of silently
# adding or losing a field. Built from the constant above, which holds no user input; the id is a
# bound parameter.
RETURN_BY_ID_SQL = f"SELECT {', '.join(RETURN_COLUMNS)} FROM public.returns WHERE id = %s"


async def get_return(conn: AsyncConnection, return_id: UUID) -> ReturnRow | None:
    """The return with this id, or None."""
    async with conn.cursor(row_factory=class_row(ReturnRow)) as cursor:
        await cursor.execute(RETURN_BY_ID_SQL, (return_id,))
        return await cursor.fetchone()


# ---------- the write side (issue #54) ----------
# The caller owns the transaction; nothing here commits. The two index names are checked against
# db/schema.sql by a test, not trusted: a rename in a migration fails that test, not production.

# The partial unique index that is R4: one active return per order line (ADR-0004, Decision 6).
ACTIVE_ORDER_LINE_INDEX = "returns_active_order_line_uq"
# The unique index on (idempotency_key, key_generation): one return per key and generation.
KEY_INDEX = "returns_idempotency_key_generation_uq"
# "Active" as a boolean SQL expression over the columns of public.returns, the predicate of the
# partial index above. A matrix test on Postgres checks that the two agree for every combination of
# status and outcome. It is always used inside parentheses, and as code, never from user input.
ACTIVE_RETURN_PREDICATE_SQL = (
    "status IN ('processing', 'pending_review') OR (status = 'decided' AND outcome = 'approve')"
)


def r4_denial(policy_version: str) -> PolicyResult:
    """The policy result stored for an R4 loser: R4 alone, a failure, a hard deny, deny only.

    It is built through the PolicyResult model, so that model's validators apply (and a malformed
    version is a ValidationError). R4 has no parameter in the policy file, so the details are empty
    and no client text can get in: the function takes only the policy version.
    """
    entry = RuleResult(
        rule_id="R4",
        passed=False,
        effect="hard_deny",
        reason_code="active_return_exists",
        details={},
    )
    return PolicyResult(
        policy_version=policy_version,
        hard_deny=True,
        allowed_outcomes=("deny",),
        rule_results=(entry,),
    )


# The latest generation of a key, with its age test done IN SQL: `now()` is the database's clock
# (the start of the caller's transaction), so there is one clock and no Python time anywhere.
# Strictly older than the TTL is expired; an age of exactly the TTL cannot be observed (the TTL is
# positive and time moves). The age is measured from created_at, not updated_at.
LOOKUP_KEY_SQL = (
    f"SELECT {', '.join(RETURN_COLUMNS)}, "
    "(now() - created_at > make_interval(secs => %(ttl)s::float8)) AS expired "
    "FROM public.returns WHERE idempotency_key = %(key)s "
    "ORDER BY key_generation DESC LIMIT 1"
)


def _check_ttl(ttl_seconds: float) -> None:
    """A TTL is a finite number greater than 0; checked BEFORE any SQL or connection use."""
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int | float)
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds must be a finite number greater than 0")


async def lookup_key(
    conn: AsyncConnection, idempotency_key: str, payload_hash: str, ttl_seconds: float
) -> KeyLookup:
    """The latest generation of a key, classified. Expiry is measured on the database clock.

    In this order: no row is `NoKey` (generation 1 is next); a row older than the TTL is
    `ExpiredKey` (a new submission, with the next generation), whatever its payload; another payload
    hash is `PayloadMismatch`; then by status: `processing` is `InFlight` (of ANY age: until P1-13
    adds staleness, nothing here reads an age to decide that), `failed` with a transient error is
    `Retryable`, everything else is `Replay`. It only reads, and it commits nothing.
    """
    _check_ttl(ttl_seconds)
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(LOOKUP_KEY_SQL, {"key": idempotency_key, "ttl": ttl_seconds})
        record = await cursor.fetchone()
    if record is None:
        return NoKey(next_generation=1)
    expired = record.pop("expired")
    row = ReturnRow(**record)
    if expired:
        return ExpiredKey(row=row, next_generation=row.key_generation + 1)
    if row.payload_hash != payload_hash:
        return PayloadMismatch(row=row)
    if row.status == "processing":
        return InFlight(row=row)
    if row.status == "failed" and row.error_type in TRANSIENT_ERROR_TYPES:
        return Retryable(row=row)
    return Replay(row=row)


# ---------- savepoints, and why `conn.transaction()` is never used ----------
# On an idle connection psycopg's `conn.transaction()` is an OUTER transaction: it issues BEGIN and
# COMMITS on exit (psycopg/transaction.py: `_outer_transaction = transaction_status == IDLE`), which
# would commit the caller's work. A savepoint is therefore plain SQL through `conn.execute`. In the
# default (non-autocommit) mode psycopg issues the implicit BEGIN before the first statement, so
# SAVEPOINT always runs inside a transaction that stays open, and nothing here ever commits.
SAVEPOINT = "quorum_sp"


def _require_transaction_owner(conn: AsyncConnection) -> None:
    """The write functions need a connection whose transaction the CALLER owns.

    In autocommit mode every statement commits by itself, SAVEPOINT fails outside a transaction
    block, and the decision row, the audit row and the compare-and-set could not be atomic. The
    pool's connections are not autocommit (app/db/pool.py), so this never fires for the real caller.
    """
    if conn.autocommit:
        raise RuntimeError(
            "needs a connection whose transaction the caller owns; autocommit is not supported"
        )


# One INSERT for a processing return and for the R4 deny row: `status` and `outcome` are
# parameters. `attempt` (database default 1), `as_of`, `created_at` and `updated_at` (default
# now()) are never supplied: the database's clock is the only clock.
INSERT_RETURN_SQL = (
    "INSERT INTO public.returns (order_id, order_line_id, category, amount_minor, currency, "
    "delivered_on, reason, status, outcome, policy_version, idempotency_key, payload_hash, "
    "key_generation) VALUES (%(order_id)s, %(order_line_id)s, %(category)s, %(amount_minor)s, "
    "%(currency)s, %(delivered_on)s, %(reason)s, %(status)s, %(outcome)s, %(policy_version)s, "
    "%(idempotency_key)s, %(payload_hash)s, %(key_generation)s) "
    f"RETURNING {', '.join(RETURN_COLUMNS)}"
)
# Does this key and generation exist? A `23505` on the active-order-line index does not say whether
# the colliding row is the same key (a replay) or another one (R4); Postgres reports whichever
# unique index it checks first, so the answer must not depend on that order.
KEY_EXISTS_SQL = (
    "SELECT 1 FROM public.returns "
    "WHERE idempotency_key = %(key)s AND key_generation = %(generation)s"
)
# The return that blocks an order line. It contains ACTIVE_RETURN_PREDICATE_SQL verbatim (a test
# recognises this statement by that text) and is read through the connection, never a side channel.
FIND_BLOCKER_SQL = (
    "SELECT id FROM public.returns WHERE order_id = %(order_id)s "
    f"AND order_line_id = %(order_line_id)s AND ({ACTIVE_RETURN_PREDICATE_SQL}) LIMIT 1"
)
DECISION_INSERT_SQL = (
    "INSERT INTO public.decisions (return_id, policy_version, outcome, decided_by, rule_results, "
    "allowed_outcomes, "
    + ", ".join(LLM_COLUMNS)
    + ") VALUES (%(return_id)s, %(policy_version)s, %(outcome)s, %(decided_by)s, %(rule_results)s, "
    "%(allowed_outcomes)s, " + ", ".join(f"%({column})s" for column in LLM_COLUMNS) + ")"
)


async def _insert_decision(
    conn: AsyncConnection, return_id: UUID, decision: DecisionRecord
) -> None:
    """The `decisions` row of a valid decision. Its policy_version is the PolicyResult's: the
    composite foreign key (id, policy_version) refuses one that differs from the return's."""
    result = decision.policy_result
    await conn.execute(
        DECISION_INSERT_SQL,
        {
            "return_id": return_id,
            "policy_version": result.policy_version,
            "outcome": decision.outcome,
            "decided_by": decision.decided_by,
            "rule_results": Jsonb(result.rule_results_json()),
            "allowed_outcomes": list(result.allowed_outcomes),
            **llm_values(decision.llm),
        },
    )


async def _run_in_savepoint(conn: AsyncConnection, run, expected: tuple[str, ...]):
    """Run `await run()` inside a savepoint: (result, None) on success, (None, constraint) when one
    of the `expected` unique indexes refused it (the savepoint is rolled back and the caller's
    transaction goes on). Every other error, a unique violation of an unknown index included,
    propagates unchanged with no cleanup: the caller's transaction decides."""
    await conn.execute(f"SAVEPOINT {SAVEPOINT}")
    try:
        result = await run()
    except UniqueViolation as violation:
        if violation.diag.constraint_name not in expected:
            raise
        await conn.execute(f"ROLLBACK TO SAVEPOINT {SAVEPOINT}")
        await conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}")
        return None, violation.diag.constraint_name
    await conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}")
    return result, None


async def _insert_returning(conn: AsyncConnection, params: dict) -> ReturnRow | None:
    async with conn.cursor(row_factory=class_row(ReturnRow)) as cursor:
        await cursor.execute(INSERT_RETURN_SQL, params)
        return await cursor.fetchone()


async def _insert_in_savepoint(
    conn: AsyncConnection, params: dict, expected: tuple[str, ...]
) -> tuple[ReturnRow | None, str | None]:
    """Insert one `returns` row in a savepoint: (row, None), or (None, constraint) when refused."""
    return await _run_in_savepoint(conn, lambda: _insert_returning(conn, params), expected)


async def _key_exists(conn: AsyncConnection, idempotency_key: str, generation: int) -> bool:
    cursor = await conn.execute(KEY_EXISTS_SQL, {"key": idempotency_key, "generation": generation})
    return await cursor.fetchone() is not None


async def _find_blocker(conn: AsyncConnection, order_id: str, order_line_id: str) -> UUID | None:
    cursor = await conn.execute(
        FIND_BLOCKER_SQL, {"order_id": order_id, "order_line_id": order_line_id}
    )
    found = await cursor.fetchone()
    return found[0] if found is not None else None


async def _store_r4_denial(
    conn: AsyncConnection, params: dict, blocker_id: UUID | None
) -> R4Denied | KeyConflict:
    """Store the loser of an R4 collision: a `decided`/`deny` row (outside the active index, so the
    insert succeeds), its decision row (decided_by rules, the one-entry R4 array) and its audit row,
    which alone names the blocker (it is never in what is returned). `blocker_id` is None only when
    no blocker could be found twice: the deny is still stored (fail closed), with a NULL id."""
    deny = {**params, "status": "decided", "outcome": "deny"}
    row, _ = await _insert_in_savepoint(conn, deny, expected=(KEY_INDEX,))
    if row is None:
        return KeyConflict()  # a racing same-key request got in first: a replay, nothing written
    decision = DecisionRecord(
        outcome="deny", decided_by="rules", policy_result=r4_denial(params["policy_version"])
    )
    await _insert_decision(conn, row.id, decision)
    await insert_audit(conn, row.id, 1, "r4_denied", conflicting_return_id=blocker_id)
    return R4Denied(row=row)


async def insert_return(
    conn: AsyncConnection,
    fields: ReturnFields,
    *,
    idempotency_key: str,
    payload_hash: str,
    key_generation: int,
    policy_version: str,
) -> InsertResult:
    """Insert a `processing` return (attempt 1); on a collision with an active return store an R4
    deny with its decision and audit rows (ADR-0004, Decision 6).

    `as_of` is never supplied (the database's now()). A violation of the key index is `KeyConflict`.
    A violation of the active-order-line index is first checked for the same key and generation
    (then it is also `KeyConflict`: a replay, whichever index Postgres reported first); otherwise
    the blocker is looked up. No blocker found means it left the active set after the collision, so
    the insert is retried ONCE; collided again, the blocker is looked up again, and if it is still
    not found the deny is stored with a NULL conflicting id. At most two inserts, never a loop.
    """
    _require_transaction_owner(conn)
    params = {
        **fields.model_dump(),
        "status": "processing",
        "outcome": None,
        "policy_version": policy_version,
        "idempotency_key": idempotency_key,
        "payload_hash": payload_hash,
        "key_generation": key_generation,
    }
    for pass_number in (1, 2):
        row, violated = await _insert_in_savepoint(
            conn, params, (KEY_INDEX, ACTIVE_ORDER_LINE_INDEX)
        )
        if row is not None:
            return Inserted(row=row)
        if violated == KEY_INDEX or await _key_exists(conn, idempotency_key, key_generation):
            return KeyConflict()
        blocker_id = await _find_blocker(conn, fields.order_id, fields.order_line_id)
        if blocker_id is None and pass_number == 1:
            continue  # the blocker left after the collision: try the insert once more
        return await _store_r4_denial(conn, params, blocker_id)
    raise AssertionError("unreachable: the second pass always returns")


# ---------- the compare-and-set transitions out of `processing` ----------
# Each one is a single UPDATE on id AND status = 'processing' AND attempt, so a stale attempt (or a
# row that is no longer processing) changes nothing: no row comes back and the result is `Lost`.
# They write only columns quorum_app may update (status, outcome, error_type, retry_after_seconds,
# attempt, updated_at). `updated_at = now()` is set by the statement itself: there is no trigger,
# and now() is the start of the caller's transaction (the database's clock).
FINALIZE_DECIDED_SQL = (
    "UPDATE public.returns SET status = %(status)s, outcome = %(outcome)s, error_type = NULL, "
    "retry_after_seconds = NULL, updated_at = now() "
    "WHERE id = %(id)s AND status = 'processing' AND attempt = %(attempt)s "
    f"RETURNING {', '.join(RETURN_COLUMNS)}"
)
FINALIZE_FAILED_SQL = (
    "UPDATE public.returns SET status = 'failed', outcome = NULL, error_type = %(error_type)s, "
    "retry_after_seconds = %(retry_after_seconds)s, updated_at = now() "
    "WHERE id = %(id)s AND status = 'processing' AND attempt = %(attempt)s "
    f"RETURNING {', '.join(RETURN_COLUMNS)}"
)
MAX_RETRY_AFTER_SECONDS = 3600  # the schema's returns_retry_after_ck: 0 to 3600


async def _update_returning(conn: AsyncConnection, sql: str, params: dict) -> ReturnRow | None:
    """Run one UPDATE ... RETURNING: the new row, or None when no row matched (the Lost case)."""
    async with conn.cursor(row_factory=class_row(ReturnRow)) as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


def _check_failure(error_type: str, retry_after_seconds: int | None) -> None:
    """Refuse a bad failure BEFORE any SQL (the database CHECKs stay as the second wall): the error
    type is one of the six, `retry_after_seconds` is None or a whole number from 0 to 3600, and the
    permanent type takes none (`returns_state_ck`)."""
    if error_type not in ERROR_TYPES:
        raise ValueError(f"unknown error type: {error_type!r}")
    if retry_after_seconds is None:
        return
    if (
        isinstance(retry_after_seconds, bool)
        or not isinstance(retry_after_seconds, int)
        or not 0 <= retry_after_seconds <= MAX_RETRY_AFTER_SECONDS
    ):
        raise ValueError("retry_after_seconds must be a whole number from 0 to 3600, or None")
    if error_type in PERMANENT_ERROR_TYPES:
        raise ValueError("a permanent failure has no retry_after_seconds")


async def finalize_decided(
    conn: AsyncConnection, *, return_id: UUID, attempt: int, decision: DecisionRecord
) -> FinalizeResult:
    """processing -> decided or pending_review, by compare-and-set on status and attempt.

    The compare-and-set comes first. Applied: the `decisions` row (the PolicyResult's policy_version
    must equal the return's, which the composite foreign key enforces: a mismatch is an error, not
    a result) and the audit row (`rules_decided`, `llm_decided`, or `escalated`), all in the
    caller's transaction. Lost: only an audit row, `superseded`, with the loser's attempt and its
    model call, and no decision.
    """
    _require_transaction_owner(conn)
    status = "pending_review" if decision.outcome == "escalate" else "decided"
    row = await _update_returning(
        conn,
        FINALIZE_DECIDED_SQL,
        {"status": status, "outcome": decision.outcome, "id": return_id, "attempt": attempt},
    )
    if row is None:
        await insert_audit(conn, return_id, attempt, "superseded", llm=decision.llm)
        return Lost()
    await _insert_decision(conn, return_id, decision)
    if decision.outcome == "escalate":
        event = "escalated"
    else:
        event = "rules_decided" if decision.decided_by == "rules" else "llm_decided"
    await insert_audit(conn, return_id, attempt, event, llm=decision.llm)
    return Applied(row=row)


async def finalize_failed(
    conn: AsyncConnection,
    *,
    return_id: UUID,
    attempt: int,
    error_type: str,
    retry_after_seconds: int | None,
    llm: LLMCallRecord | None = None,
) -> FinalizeResult:
    """processing -> failed, by compare-and-set on status and attempt.

    Applied: an audit row `failed_<error_type>` carrying the model call (kept also when it failed),
    and NO decision row: a decision exists only for a valid decision. Lost: `superseded`, not the
    failed event. The inputs are validated first (ValueError before any SQL).
    """
    _check_failure(error_type, retry_after_seconds)
    _require_transaction_owner(conn)
    row = await _update_returning(
        conn,
        FINALIZE_FAILED_SQL,
        {
            "error_type": error_type,
            "retry_after_seconds": retry_after_seconds,
            "id": return_id,
            "attempt": attempt,
        },
    )
    if row is None:
        await insert_audit(conn, return_id, attempt, "superseded", llm=llm)
        return Lost()
    await insert_audit(conn, return_id, attempt, failed_event(error_type), llm=llm)
    return Applied(row=row)


# ---------- failed -> processing, the retry ----------
# Only a TRANSIENT failure can be retried: the transient set is a PARAMETER (from vocabulary, the
# one definition), never a literal. The compare-and-set is on id, status = 'failed' AND the attempt
# that was read. `attempt + 1` marks the new attempt; the error and the retry hint are cleared in
# the same statement because a `processing` row must carry neither (returns_state_ck).
RETRY_FAILED_SQL = (
    "UPDATE public.returns SET status = 'processing', attempt = attempt + 1, error_type = NULL, "
    "retry_after_seconds = NULL, updated_at = now() "
    "WHERE id = %(id)s AND status = 'failed' AND attempt = %(attempt)s "
    "AND error_type = ANY(%(transient)s::text[]) "
    f"RETURNING {', '.join(RETURN_COLUMNS)}"
)
# When re-entering `processing` hits the active-order-line index, the retry ends as an R4 deny. It
# was an attempt, so `attempt` advances too (a small stretch of ADR-0004, which says attempt
# increments when a row enters `processing`: the audit row, the return and the attempt agree).
RETRY_DENY_SQL = (
    "UPDATE public.returns SET status = 'decided', outcome = 'deny', error_type = NULL, "
    "retry_after_seconds = NULL, attempt = attempt + 1, updated_at = now() "
    "WHERE id = %(id)s AND status = 'failed' AND attempt = %(attempt)s "
    "AND error_type = ANY(%(transient)s::text[]) "
    f"RETURNING {', '.join(RETURN_COLUMNS)}"
)


async def retry_failed(conn: AsyncConnection, row: ReturnRow) -> RetryResult:
    """failed (transient) -> processing with attempt + 1; or an R4 deny if the index is hit.

    `row` is the return as the caller read it: its id and attempt are the compare-and-set. No row
    matched (a permanent failure, a row that is not failed, a stale attempt, a concurrent retry that
    got there first): `Lost`, and NO audit row, because this attempt never entered `processing` and
    has no attempt number of its own. A `23505` on the active-order-line index (the only index a
    status change can hit) is handled like the insert: find the blocker, retry the update ONCE if
    it is gone, and store the deny (decision row and `r4_denied` audit row of attempt n + 1, with
    the blocker's id, NULL if it was never found) in the caller's transaction.
    """
    _require_transaction_owner(conn)
    params = {"id": row.id, "attempt": row.attempt, "transient": sorted(TRANSIENT_ERROR_TYPES)}
    for pass_number in (1, 2):
        updated, violated = await _run_in_savepoint(
            conn,
            lambda: _update_returning(conn, RETRY_FAILED_SQL, params),
            (ACTIVE_ORDER_LINE_INDEX,),
        )
        if violated is None:
            return Retried(row=updated) if updated is not None else Lost()
        blocker_id = await _find_blocker(conn, row.order_id, row.order_line_id)
        if blocker_id is None and pass_number == 1:
            continue  # the blocker left after the collision: try the update once more
        denied = await _update_returning(conn, RETRY_DENY_SQL, params)
        if denied is None:
            return Lost()
        decision = DecisionRecord(
            outcome="deny", decided_by="rules", policy_result=r4_denial(row.policy_version)
        )
        await _insert_decision(conn, row.id, decision)
        await insert_audit(
            conn, row.id, row.attempt + 1, "r4_denied", conflicting_return_id=blocker_id
        )
        return R4Denied(row=denied)
    raise AssertionError("unreachable: the second pass always returns")
