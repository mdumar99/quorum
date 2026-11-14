"""The `returns` table: columns, checks and the state rule (#44, criteria S1 and S2).

Sources: ADR-0004 D1, D2, D4, D5, D6 and ADR-0003 D2, as the approved schema proposal on #44 reads
them. Needs the stack's Postgres with the bootstrap applied and the migrations at head (including
0002). DATABASE_URL must name quorum_app. Every test runs as quorum_app through app_conn, in a
transaction that is rolled back.
"""

import importlib.util
from itertools import product
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors
from schema_helpers import OMIT, insert_return, rejected, require_relation

pytestmark = pytest.mark.integration

API_DIR = Path(__file__).resolve().parents[2]
MIGRATION = API_DIR / "migrations" / "versions" / "0002_returns_decisions_audit_log.py"

TRANSIENT_ERRORS = ("llm_timeout", "rate_limited", "provider_unavailable", "capacity", "abandoned")
ALL_ERRORS = (*TRANSIENT_ERRORS, "invalid_llm_output")

# name: (data_type, is_nullable, column_default), from the approved column lines.
EXPECTED_RETURNS_COLUMNS = {
    "id": ("uuid", "NO", "gen_random_uuid()"),
    "order_id": ("text", "NO", None),
    "order_line_id": ("text", "NO", None),
    "category": ("text", "NO", None),
    "amount_minor": ("bigint", "NO", None),
    "currency": ("text", "NO", None),
    "delivered_on": ("date", "NO", None),
    "reason": ("text", "NO", None),
    "status": ("text", "NO", None),
    "outcome": ("text", "YES", None),
    "error_type": ("text", "YES", None),
    "retry_after_seconds": ("integer", "YES", None),
    "attempt": ("integer", "NO", "1"),
    "as_of": ("timestamp with time zone", "NO", "now()"),
    "policy_version": ("text", "NO", None),
    "idempotency_key": ("text", "NO", None),
    "payload_hash": ("text", "NO", None),
    "key_generation": ("integer", "NO", "1"),
    "created_at": ("timestamp with time zone", "NO", "now()"),
    "updated_at": ("timestamp with time zone", "NO", "now()"),
}


# ---------- S1: the migration ----------


def test_migration_0002_follows_the_baseline_and_has_no_downgrade():
    assert MIGRATION.exists(), f"migration 0002 does not exist yet: {MIGRATION.name}"
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0002"
    assert module.down_revision == "0001"
    assert not hasattr(module, "downgrade"), "migrations are forward-only (ADR-0002, Decision 1)"
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert script.get_heads() == ["0002"]


# ---------- S2: columns ----------


def test_returns_columns_types_defaults_match_the_proposal(app_conn):
    require_relation(app_conn, "returns")
    found = {
        name: (data_type, nullable, default)
        for name, data_type, nullable, default in app_conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'returns'"
        ).fetchall()
    }
    assert found == EXPECTED_RETURNS_COLUMNS


def test_returns_id_is_a_version_4_uuid_and_a_non_v4_id_is_rejected(app_conn):
    return_id = str(insert_return(app_conn))
    assert return_id[14] == "4" and return_id[19] in "89ab"
    good = UUID("00000000-0000-4000-8000-000000000000")
    assert insert_return(app_conn, id=good) == good
    for bad in ("00000000-0000-1000-8000-000000000000", "00000000-0000-4000-c000-000000000000"):
        with rejected(app_conn, errors.CheckViolation, "returns_id_v4_ck"):
            insert_return(app_conn, id=UUID(bad))


def test_timestamps_come_from_the_database_clock(app_conn):
    return_id = insert_return(app_conn)
    equal_to_now = app_conn.execute(
        "SELECT as_of = now(), created_at = now(), updated_at = now()"
        " FROM public.returns WHERE id = %s",
        (return_id,),
    ).fetchone()
    assert equal_to_now == (True, True, True)


# ---------- S2: the checks ----------


# Postgres checks CHECK constraints in name order and reports the first one that fails, so the
# exact names below rely on that: returns_error_type_ck, returns_outcome_ck, returns_retry_after_ck
# and returns_status_ck each sort before returns_state_ck, which also judges these rows.
@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        pytest.param({"status": "bogus"}, "returns_status_ck", id="unknown-status"),
        pytest.param(
            {"status": "decided", "outcome": "maybe"}, "returns_outcome_ck", id="unknown-outcome"
        ),
        pytest.param(
            {"status": "failed", "error_type": "weird"},
            "returns_error_type_ck",
            id="unknown-error-type",
        ),
    ],
)
def test_returns_rejects_unknown_status_outcome_error_type(app_conn, values, constraint):
    with rejected(app_conn, errors.CheckViolation, constraint):
        insert_return(app_conn, **values)


def test_reason_limit_is_2000_characters_not_bytes(app_conn):
    for reason in ("r" * 2000, "é" * 2000):  # the second is 4,000 bytes
        insert_return(app_conn, reason=reason)
    for reason in ("r" * 2001, "é" * 2001):
        with rejected(app_conn, errors.CheckViolation, "returns_reason_length_ck"):
            insert_return(app_conn, reason=reason)


def test_retry_after_seconds_range_0_to_3600(app_conn):
    failed = {"status": "failed", "error_type": "llm_timeout"}
    for seconds in (0, 3600):
        insert_return(app_conn, retry_after_seconds=seconds, **failed)
    for seconds in (-1, 3601):
        with rejected(app_conn, errors.CheckViolation, "returns_retry_after_ck"):
            insert_return(app_conn, retry_after_seconds=seconds, **failed)


def test_attempt_and_key_generation_must_be_at_least_1(app_conn):
    return_id = insert_return(app_conn, attempt=OMIT, key_generation=OMIT)
    defaults = app_conn.execute(
        "SELECT attempt, key_generation FROM public.returns WHERE id = %s", (return_id,)
    ).fetchone()
    assert defaults == (1, 1)
    with rejected(app_conn, errors.CheckViolation, "returns_attempt_ck"):
        insert_return(app_conn, attempt=0)
    with rejected(app_conn, errors.CheckViolation, "returns_key_generation_ck"):
        insert_return(app_conn, key_generation=0)


def test_amount_minor_cannot_be_negative(app_conn):
    insert_return(app_conn, amount_minor=0)
    with rejected(app_conn, errors.CheckViolation, "returns_amount_minor_ck"):
        insert_return(app_conn, amount_minor=-1)


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("order_id", "returns_order_id_length_ck"),
        ("order_line_id", "returns_order_line_id_length_ck"),
        ("idempotency_key", "returns_idempotency_key_length_ck"),
    ],
)
def test_order_id_order_line_id_and_idempotency_key_are_bounded_1_to_255(
    app_conn, column, constraint
):
    for value in ("x", "x" * 255):
        insert_return(app_conn, **{column: value})
    for value in ("", "x" * 256):
        with rejected(app_conn, errors.CheckViolation, constraint):
            insert_return(app_conn, **{column: value})


# ---------- S2: the state rule (returns_state_ck) ----------


def derived_state_is_allowed(status, outcome, error_type, retry_after_seconds):
    """The states derived from ADR-0004 D2 and D6, as in section 3 A of the approved proposal."""
    if status == "processing":
        return outcome is None and error_type is None and retry_after_seconds is None
    if status == "decided":
        return outcome in ("approve", "deny") and error_type is None and retry_after_seconds is None
    if status == "pending_review":
        return outcome == "escalate" and error_type is None and retry_after_seconds is None
    # failed: no outcome, an error type; a retry hint only for a transient error
    return (
        outcome is None
        and error_type is not None
        and (error_type != "invalid_llm_output" or retry_after_seconds is None)
    )


def test_returns_state_check_allows_exactly_the_derived_combinations(app_conn):
    mismatches = []
    combinations = product(
        ("processing", "decided", "pending_review", "failed"),
        (None, "approve", "deny", "escalate"),
        (None, *ALL_ERRORS),
        (None, 0, 30, 3600),
    )
    for status, outcome, error_type, retry in combinations:
        expected = derived_state_is_allowed(status, outcome, error_type, retry)
        try:
            with app_conn.transaction():
                insert_return(
                    app_conn,
                    status=status,
                    outcome=outcome,
                    error_type=error_type,
                    retry_after_seconds=retry,
                )
            got, violated = True, None
        except errors.CheckViolation as exc:
            got, violated = False, exc.diag.constraint_name
        if got != expected or (not got and violated != "returns_state_ck"):
            mismatches.append((status, outcome, error_type, retry, got, violated))
    assert not mismatches


FORBIDDEN_STATES = {
    "processing-with-an-outcome": {"status": "processing", "outcome": "approve"},
    "processing-with-an-error": {"status": "processing", "error_type": "capacity"},
    "processing-with-a-retry-hint": {"status": "processing", "retry_after_seconds": 30},
    "decided-with-escalate": {"status": "decided", "outcome": "escalate"},
    "decided-without-an-outcome": {"status": "decided"},
    "decided-with-an-error": {"status": "decided", "outcome": "deny", "error_type": "capacity"},
    "decided-with-a-retry-hint": {"status": "decided", "outcome": "deny", "retry_after_seconds": 5},
    "pending-review-with-approve": {"status": "pending_review", "outcome": "approve"},
    "pending-review-without-an-outcome": {"status": "pending_review"},
    "failed-with-an-outcome": {"status": "failed", "outcome": "deny", "error_type": "capacity"},
    "failed-without-an-error": {"status": "failed"},
    "permanent-error-with-a-retry-hint": {
        "status": "failed",
        "error_type": "invalid_llm_output",
        "retry_after_seconds": 5,
    },
}


@pytest.mark.parametrize("values", FORBIDDEN_STATES.values(), ids=FORBIDDEN_STATES.keys())
def test_returns_state_check_rejects_forbidden_combinations(app_conn, values):
    with rejected(app_conn, errors.CheckViolation, "returns_state_ck"):
        insert_return(app_conn, **values)
