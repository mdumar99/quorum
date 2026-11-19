"""Builders, readers and async helpers for the committing integration tests of #54.

Setup is done as the OWNER with plain SQL (so each test exercises one repository function, not the
chain), and the functions under test run as `quorum_app` on their own connections. The tests are
sync functions that call `run(...)` (asyncio.run): no pytest-asyncio, no new dependency.

Every row a test makes carries the run marker at the start of the key and of the order id, so the
cleanup of repo_fixtures can find it and nothing else.
"""

import asyncio
import hashlib
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
    url: str, *, cursor_factory=None, isolation=None, lock_timeout_ms: int = 15000
):
    """A connection as quorum_app. Rolled back and closed at the end: a test commits explicitly.

    `lock_timeout_ms` is applied as the libpq option `-c lock_timeout=<n>`: a lock wait that nobody
    releases ends in `psycopg.errors.LockNotAvailable` instead of a hang. The default is longer than
    the 10 s timeout of `wait_until_blocked`, so a deliberate wait is never cut short by it.
    """
    kwargs = {"cursor_factory": cursor_factory} if cursor_factory is not None else {}
    conn = await AsyncConnection.connect(
        url, options=f"-c lock_timeout={lock_timeout_ms}", **kwargs
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
