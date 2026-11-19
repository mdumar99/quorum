"""Builders, readers and async helpers for the committing integration tests of #54.

Setup is done as the OWNER with plain SQL (so each test exercises one repository function, not the
chain), and the functions under test run as `quorum_app` on their own connections. The tests are
sync functions that call `run(...)` (asyncio.run): no pytest-asyncio, no new dependency.

Every row a test makes carries the run marker at the start of the key and of the order id, so the
cleanup of repo_fixtures can find it and nothing else.
"""

import asyncio
import hashlib
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row
from schema_helpers import insert_return

from app.db.models import DecisionRecord, ReturnFields
from app.llm.capture import capture_raw_output
from app.llm.types import LLMCallRecord
from app.policy import PolicyCase, PolicyResult, evaluate, load_current_policy

POLICY = load_current_policy()
PV = POLICY.policy_version  # the policy version a return is pinned with in these tests
AS_OF = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
HASH_A = hashlib.sha256(b"payload-a").hexdigest()
HASH_B = hashlib.sha256(b"payload-b").hexdigest()
TTL = 100.0  # seconds; small, so a test can backdate a row past it

ALL_EVENT_TYPES = (
    "rules_decided",
    "llm_decided",
    "escalated",
    "r4_denied",
    "superseded",
    "failed_llm_timeout",
    "failed_rate_limited",
    "failed_provider_unavailable",
    "failed_capacity",
    "failed_abandoned",
    "failed_invalid_llm_output",
)
SIX_ERROR_TYPES = (
    "llm_timeout",
    "rate_limited",
    "provider_unavailable",
    "capacity",
    "abandoned",
    "invalid_llm_output",
)
TRANSIENT_FIVE = SIX_ERROR_TYPES[:5]
LLM_COLUMNS = (
    "provider",
    "model_id",
    "prompt_version",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "raw_output",
    "raw_output_bytes",
    "raw_output_sha256",
    "raw_output_truncated",
)
R4_ARRAY = [
    {
        "rule_id": "R4",
        "passed": False,
        "effect": "hard_deny",
        "reason_code": "active_return_exists",
        "details": {},
    }
]


def run(coroutine):
    """Run one coroutine from a sync test."""
    return asyncio.run(coroutine)


# ---------- names and rows ----------


def key_for(marker: str, name: str = "a") -> str:
    return f"{marker}key-{name}"


def order_for(marker: str) -> str:
    return f"{marker}order"


def fields_for(marker: str, line: str = "1", **overrides) -> ReturnFields:
    values = {
        "order_id": order_for(marker),
        "order_line_id": f"line-{line}",
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "USD",
        "delivered_on": date(2026, 11, 1),
        "reason": "does not fit",
    }
    values.update(overrides)
    return ReturnFields(**values)


def new_return(owner, marker: str, *, name: str = "a", line: str = "1", **columns) -> UUID:
    """A committed return made by the owner. `columns` override any column (status, outcome,
    error_type, retry_after_seconds, attempt, key_generation, created_at, payload_hash...)."""
    values = {
        "order_id": order_for(marker),
        "order_line_id": f"line-{line}",
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "USD",
        "delivered_on": date(2026, 11, 1),
        "reason": "does not fit",
        "idempotency_key": key_for(marker, name),
        "payload_hash": HASH_A,
        "policy_version": PV,
    }
    values.update(columns)
    return_id = insert_return(owner, **values)
    owner.commit()
    return return_id


def owner_row(owner, return_id: UUID) -> dict:
    with owner.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM public.returns WHERE id = %s", (return_id,))
        row = cursor.fetchone()
    owner.rollback()  # end the read transaction: no stale snapshot, no lock held
    return row


def decision_rows(owner, return_id: UUID) -> list[dict]:
    with owner.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM public.decisions WHERE return_id = %s", (return_id,))
        rows = cursor.fetchall()
    owner.rollback()
    return rows


def audit_rows(owner, return_id: UUID) -> list[dict]:
    with owner.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM public.audit_log WHERE return_id = %s ORDER BY id", (return_id,)
        )
        rows = cursor.fetchall()
    owner.rollback()
    return rows


def count_by_key(owner, key: str) -> int:
    count = owner.execute(
        "SELECT count(*) FROM public.returns WHERE idempotency_key = %s", (key,)
    ).fetchone()[0]
    owner.rollback()
    return count


def backdate_created(owner, return_id: UUID, seconds: float) -> None:
    """Make a return older: created_at = the database's now() minus `seconds` (as the owner)."""
    owner.execute(
        "UPDATE public.returns SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
        (seconds, return_id),
    )
    owner.commit()


def backdate_updated(owner, return_id: UUID, seconds: float) -> None:
    owner.execute(
        "UPDATE public.returns SET updated_at = now() - make_interval(secs => %s) WHERE id = %s",
        (seconds, return_id),
    )
    owner.commit()


# ---------- policy and model-call records ----------


def policy_result(category: str = "shoes") -> PolicyResult:
    """A real result of the real policy: `shoes` passes every rule, `digital` is a hard deny."""
    case = PolicyCase(
        category=category, amount_minor=1999, currency="USD", delivered_on=date(2026, 11, 1)
    )
    return evaluate(POLICY, case, AS_OF)


def llm_call(raw: str = '{"outcome": "approve", "rationale": "fits the policy"}') -> LLMCallRecord:
    return LLMCallRecord(
        provider="fake",
        model_id="fake-v1",
        prompt_version="pv:1",
        latency_ms=12,
        prompt_tokens=10,
        completion_tokens=5,
        raw_output=capture_raw_output(raw),
    )


def rules_decision(outcome: str = "deny", category: str = "digital") -> DecisionRecord:
    return DecisionRecord(
        outcome=outcome, decided_by="rules", policy_result=policy_result(category)
    )


def llm_decision(outcome: str = "approve", category: str = "shoes") -> DecisionRecord:
    return DecisionRecord(
        outcome=outcome,
        decided_by="llm",
        policy_result=policy_result(category),
        llm=llm_call(),
    )


# ---------- async connections as quorum_app ----------


@asynccontextmanager
async def app_connection(
    url: str,
    *,
    cursor_factory=None,
    isolation=None,
    lock_timeout_ms: int = 15000,
    autocommit: bool = False,
):
    """A connection as quorum_app. Rolled back and closed at the end: a test commits explicitly.

    `lock_timeout_ms` is applied as the libpq option `-c lock_timeout=<n>`: a lock wait that nobody
    releases ends in `psycopg.errors.LockNotAvailable` instead of a hang. The default is longer than
    the 10 s timeout of `wait_until_blocked`, so a deliberate wait is never cut short by it.
    `autocommit` (issue #56) opens the connection in autocommit mode; the default is off, as before.
    """
    kwargs = {"cursor_factory": cursor_factory} if cursor_factory is not None else {}
    conn = await AsyncConnection.connect(
        url, options=f"-c lock_timeout={lock_timeout_ms}", autocommit=autocommit, **kwargs
    )
    try:
        if isolation is not None:
            await conn.set_isolation_level(isolation)
        yield conn
    finally:
        await conn.rollback()
        await conn.close()


async def with_conn(url: str, action, *, commit: bool = True, **kwargs):
    """Open a connection, run `await action(conn)`, commit, close; return the result."""
    async with app_connection(url, **kwargs) as conn:
        result = await action(conn)
        if commit:
            await conn.commit()
        return result


class HeldWrite:
    """The handle `held_write` gives the body: the action's result and the way to commit it."""

    def __init__(self, conn: AsyncConnection, result):
        self.conn = conn
        self.result = result
        self.committed = False

    async def commit(self) -> None:
        await self.conn.commit()
        self.committed = True


@asynccontextmanager
async def held_write(url: str, action, *, lock_timeout_ms: int = 2000):
    """Run `await action(conn)` on its OWN quorum_app connection and HOLD the transaction: nothing
    is committed until the body calls `await held.commit()` (issue #56).

    `action` is an async callable that takes the connection, for example
    `lambda c: finalize_decided(c, ...)`, `lambda c: insert_return(c, ...)` or
    `lambda c: retry_failed(c, row)`; its result is `held.result`. At exit the transaction is rolled
    back and the connection closed (by `app_connection`), also when the body raises and also when
    the action itself raises, so the owner cleanup never waits on a lock this connection holds. The
    connection has a `lock_timeout` (2 s by default): a wait that nobody releases ends in
    `LockNotAvailable` and not in a hang.
    """
    async with app_connection(url, lock_timeout_ms=lock_timeout_ms) as conn:
        yield HeldWrite(conn, await action(conn))


STATE_TABLES = ("returns", "decisions", "audit_log")


async def table_state(conn: AsyncConnection, return_id: UUID) -> dict:
    """What the CALLER'S OWN connection sees of the tables, in its own transaction (issue #56): the
    count of each of the three tables, and the `xmin` of the return row and of its decision row
    (None when there is none). It is read on the connection under test and not through an owner
    connection, because an UPDATE or INSERT that the function made and did not commit is invisible
    to every other connection and visible to this one. `xmin` is the id of the transaction that
    wrote the row version, so it changes on ANY update, also one that sets a column to itself.
    """
    state = {}
    for table in STATE_TABLES:  # a fixed tuple, never user input
        cursor = await conn.execute(f"SELECT count(*) FROM public.{table}")
        state[f"{table}_count"] = (await cursor.fetchone())[0]
    cursor = await conn.execute("SELECT xmin::text FROM public.returns WHERE id = %s", (return_id,))
    found = await cursor.fetchone()
    state["return_xmin"] = found[0] if found else None
    cursor = await conn.execute(
        "SELECT xmin::text FROM public.decisions WHERE return_id = %s", (return_id,)
    )
    found = await cursor.fetchone()
    state["decision_xmin"] = found[0] if found else None
    return state


# ---------- deterministic interleavings ----------


async def wait_until_blocked(owner_url: str, pid: int, timeout: float = 10.0) -> None:
    """Wait until backend `pid` is blocked by another backend: `pg_blocking_pids(pid)` is not empty.

    `pg_stat_activity` is NOT used: it hides other roles' sessions from a non-superuser (state,
    wait event and query are NULL or "<insufficient privilege>"), and the monitor connects as
    quorum_owner while the sessions under test are quorum_app. `pg_blocking_pids` answers for any
    backend whatever the role. The monitor is its own autocommit connection, so each poll sees
    the current state. Raises AssertionError after `timeout` seconds: a race that was not
    exercised is not a pass.
    """
    deadline = time.monotonic() + timeout
    with psycopg.connect(owner_url, autocommit=True) as monitor:
        while time.monotonic() < deadline:
            (blockers,) = monitor.execute("SELECT pg_blocking_pids(%s::int)", (pid,)).fetchone()
            if blockers:
                return
            await asyncio.sleep(0.02)
    raise AssertionError("the second connection never waited on a lock: the race was not exercised")


def is_returns_insert(text: str) -> bool:
    """True for a statement that inserts into `returns`: it starts with INSERT INTO, mentions
    `returns`, and mentions neither `decisions` nor `audit_log`."""
    flat = " ".join(text.split()).upper()
    return (
        flat.startswith("INSERT INTO")
        and "RETURNS" in flat
        and "DECISIONS" not in flat
        and "AUDIT_LOG" not in flat
    )


def is_returns_update(text: str) -> bool:
    """True for a statement that updates `returns`: it starts with UPDATE and mentions `returns`."""
    flat = " ".join(text.split()).upper()
    return flat.startswith("UPDATE") and "RETURNS" in flat


# The words that make a statement more than a plain read: data changes, DDL, privileges, a
# transaction or savepoint command, SELECT ... INTO (which creates a table) and SET.
_NOT_A_READ = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|COPY|CREATE|DROP|ALTER|GRANT|REVOKE|CALL|LOCK|INTO"
    r"|SAVEPOINT|RELEASE|ROLLBACK|COMMIT|BEGIN|SET|RESET)\b",
    re.IGNORECASE,
)
# The four row-locking clauses of SELECT. (UPDATE is also caught by the list above; SHARE is not.)
_LOCKING_CLAUSE = re.compile(
    r"\bFOR\s+(NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)\b", re.IGNORECASE
)
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$")
_ESCAPE_STRING = re.compile(r"(?<![A-Za-z0-9_])[eE]'")


def _is_identifier_char(char: str) -> bool:
    """A character that can be part of a PostgreSQL identifier: a letter, a digit, `_`, `$`, or any
    non-ASCII character. A `$` right after one is part of the identifier (`a$x$` is a name), not
    the start of a dollar quote: a dollar quote after an identifier needs whitespace between."""
    return char.isalnum() or char in "_$" or ord(char) > 127


def _blank_comments_and_literals(text: str) -> str | None:
    """`text` with comments replaced by a space and string literals, quoted identifiers and
    dollar-quoted strings replaced by an empty literal; None when it cannot be read to the end
    (an unterminated comment, string or dollar quote)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        two = text[i : i + 2]
        if two == "--":
            end = text.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
        elif two == "/*":
            depth, i = 1, i + 2
            while depth and i < n:
                if text[i : i + 2] == "/*":
                    depth, i = depth + 1, i + 2
                elif text[i : i + 2] == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                return None
            out.append(" ")
        elif text[i] in ("'", '"'):
            quote, i = text[i], i + 1
            while True:
                end = text.find(quote, i)
                if end == -1:
                    return None
                if text[end : end + 2] == quote * 2:  # a doubled quote is an escaped quote
                    i = end + 2
                    continue
                i = end + 1
                break
            out.append(quote * 2)
        elif (
            text[i] == "$"
            and (i == 0 or not _is_identifier_char(text[i - 1]))
            and (tag := _DOLLAR_TAG.match(text, i))
        ):
            end = text.find(tag.group(), tag.end())
            if end == -1:
                return None
            i = end + len(tag.group())
            out.append("''")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def is_plain_select(text: str) -> bool:
    """True only for ONE statement that is a plain SELECT: after comments are dropped and string
    literals are blanked, it starts with SELECT (any case, leading whitespace or comments allowed),
    holds no `;` except one at the end, none of the words in `_NOT_A_READ` (INSERT, UPDATE, DELETE,
    SAVEPOINT, INTO, SET and the like), and none of the four locking clauses (FOR UPDATE, FOR NO KEY
    UPDATE, FOR SHARE, FOR KEY SHARE, in any case and across line breaks). A data-modifying CTE
    (WITH ... INSERT/UPDATE/DELETE ...) is refused because it does not start with SELECT and holds
    the words; so is every WITH, VALUES, TABLE, EXPLAIN and parenthesised statement, even a harmless
    one: the predicate is deliberately conservative, a false "no" costs a look and a false "yes"
    costs the guarantee.

    WHAT IT CANNOT DETECT. It reads TEXT, not effects. A function call with a side effect inside a
    plain SELECT passes (`SELECT nextval('s')`, `SELECT set_config(...)`, `SELECT
    pg_advisory_lock(1)`, a user function that writes), and so does a view or rule that writes. It
    does not understand backslash-escape strings (E'...'): they are refused, not read. Text it
    cannot read to the end is refused. So it is one guard among several, not the proof that a
    function writes nothing: the others are the exact-SQL test, the row counts and `xmin` checks,
    and the fact that `quorum_app` has only SELECT and INSERT on `decisions`.
    """
    if _ESCAPE_STRING.search(text):
        return False
    cleaned = _blank_comments_and_literals(text)
    if cleaned is None:
        return False
    flat = " ".join(cleaned.split())
    if flat.endswith(";"):
        flat = flat[:-1].rstrip()
    if not re.match(r"select\b", flat, re.IGNORECASE) or ";" in flat:
        return False
    return not (_NOT_A_READ.search(flat) or _LOCKING_CLAUSE.search(flat))


def statement_hook(trigger: str, callback):
    """A cursor class for `app_connection(cursor_factory=...)` that runs `callback()` once, just
    before the first statement whose text contains `trigger`. It is how a test changes the world
    BETWEEN two statements of one repository call (for example between an R4 collision and the
    lookup of the other return).

    Returns (cursor_class, fired, seen). `seen` lists the text of EVERY statement the cursor ran,
    in order. `fired` is empty until the hook runs, and then holds one number: the position in
    `seen` of the statement that triggered it. So `seen[:fired[0]]` ran before the callback and
    `seen[fired[0]:]` ran after it (the triggering statement is the first of those)."""
    fired: list[int] = []
    seen: list[str] = []

    class HookedCursor(AsyncCursor):
        async def execute(self, query, params=None, **kwargs):
            text = query if isinstance(query, str) else query.as_string(self.connection)
            is_trigger = bool(trigger) and trigger in text and not fired
            if is_trigger:
                fired.append(len(seen))
            seen.append(text)
            if is_trigger:
                callback()
            return await super().execute(query, params, **kwargs)

    return HookedCursor, fired, seen
