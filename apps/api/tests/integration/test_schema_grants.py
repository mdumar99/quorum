"""What quorum_app may and may not do on the new tables (#44, criteria G1 to G4).

Sources: ADR-0002 D3 (default privileges give SELECT and INSERT; UPDATE is a column-level opt-in;
DELETE and TRUNCATE are never granted) and ADR-0004 D6 (the six UPDATE columns). Every test runs as
quorum_app through app_conn, in a transaction that is rolled back.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import errors, sql
from schema_helpers import (
    insert_audit,
    insert_decision,
    insert_return,
    rejected,
    require_relation,
)

pytestmark = pytest.mark.integration

TABLES = ("returns", "decisions", "audit_log")

# The 22 CHECK constraints the approved proposal names. Each one has a rejecting test in this
# folder (returns: test_returns_schema.py; decisions: test_decisions_schema.py; audit_log:
# test_audit_log_schema.py). A CHECK added to a table without a test here makes the test below fail.
EXERCISED_CHECKS = [
    "returns_id_v4_ck",
    "returns_order_id_length_ck",
    "returns_order_line_id_length_ck",
    "returns_idempotency_key_length_ck",
    "returns_amount_minor_ck",
    "returns_status_ck",
    "returns_outcome_ck",
    "returns_error_type_ck",
    "returns_reason_length_ck",
    "returns_retry_after_ck",
    "returns_attempt_ck",
    "returns_key_generation_ck",
    "returns_state_ck",
    "decisions_outcome_ck",
    "decisions_decided_by_ck",
    "decisions_allowed_outcomes_ck",
    "decisions_llm_fields_ck",
    "decisions_raw_output_ck",
    "audit_log_attempt_ck",
    "audit_log_event_type_ck",
    "audit_log_llm_call_ck",
    "audit_log_raw_output_ck",
]

# A valid new value for every returns column that quorum_app may NOT update (14 of the 20).
OTHER_RETURNS_COLUMNS = {
    "id": uuid4(),
    "order_id": "other-order",
    "order_line_id": "other-line",
    "category": "other",
    "amount_minor": 1,
    "currency": "USD",
    "delivered_on": date(2026, 11, 2),
    "reason": "other",
    "as_of": datetime(2026, 11, 2, tzinfo=UTC),
    "policy_version": "other-version:ffff",
    "idempotency_key": "other-key",
    "payload_hash": "f" * 64,
    "key_generation": 2,
    "created_at": datetime(2026, 11, 2, tzinfo=UTC),
}


# ---------- G1: SELECT and INSERT ----------


def test_select_and_insert_work_on_all_three_tables(app_conn):
    return_id = insert_return(app_conn)
    insert_decision(app_conn, return_id)
    audit_id = insert_audit(app_conn, return_id)  # no sequence grant is needed for the identity id
    assert isinstance(audit_id, int) and audit_id > 0
    keys = {"returns": "id", "decisions": "return_id", "audit_log": "return_id"}
    for table in TABLES:
        query = sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
            sql.Identifier("public", table), sql.Identifier(keys[table])
        )
        assert app_conn.execute(query, (return_id,)).fetchone()[0] == 1


# ---------- G2: column-level UPDATE on returns ----------


def test_update_works_for_each_of_the_six_granted_columns(app_conn):
    # Three valid transitions that together set status, outcome, error_type,
    # retry_after_seconds, attempt and updated_at.
    decided = insert_return(app_conn)
    failed = insert_return(app_conn)
    retried = insert_return(
        app_conn, status="failed", error_type="llm_timeout", retry_after_seconds=30
    )
    statements = [
        (
            "UPDATE public.returns SET status = 'decided', outcome = 'deny', updated_at = now()"
            " WHERE id = %s",
            decided,
        ),
        (
            "UPDATE public.returns SET status = 'failed', error_type = 'llm_timeout',"
            " retry_after_seconds = 30 WHERE id = %s",
            failed,
        ),
        (
            "UPDATE public.returns SET status = 'processing', attempt = 2, error_type = NULL,"
            " retry_after_seconds = NULL WHERE id = %s",
            retried,
        ),
    ]
    for statement, return_id in statements:
        assert app_conn.execute(statement, (return_id,)).rowcount == 1
    stored = app_conn.execute(
        "SELECT status, outcome, error_type, retry_after_seconds, attempt FROM public.returns"
        " WHERE id = ANY(%s) ORDER BY attempt, status",
        ([decided, failed, retried],),
    ).fetchall()
    assert stored == [
        ("decided", "deny", None, None, 1),
        ("failed", None, "llm_timeout", 30, 1),
        ("processing", None, None, None, 2),
    ]


@pytest.mark.parametrize("column", OTHER_RETURNS_COLUMNS)
def test_update_of_every_other_returns_column_is_denied(app_conn, column):
    return_id = insert_return(app_conn)
    statement = sql.SQL("UPDATE public.returns SET {} = %s WHERE id = %s").format(
        sql.Identifier(column)
    )
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        with app_conn.transaction():
            app_conn.execute(statement, (OTHER_RETURNS_COLUMNS[column], return_id))


# ---------- G3: what is never granted ----------


def test_update_on_decisions_and_audit_log_is_denied(app_conn):
    return_id = insert_return(app_conn)
    insert_decision(app_conn, return_id)
    insert_audit(app_conn, return_id)
    with rejected(app_conn, errors.InsufficientPrivilege):
        app_conn.execute(
            "UPDATE public.decisions SET outcome = 'deny' WHERE return_id = %s", (return_id,)
        )
    with rejected(app_conn, errors.InsufficientPrivilege):
        app_conn.execute(
            "UPDATE public.audit_log SET event_type = 'x' WHERE return_id = %s", (return_id,)
        )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("verb", ["DELETE FROM", "TRUNCATE"])
def test_delete_and_truncate_are_denied_on_all_three_tables(app_conn, verb, table):
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        with app_conn.transaction():
            app_conn.execute(
                sql.SQL("{} {}").format(sql.SQL(verb), sql.Identifier("public", table))
            )


def test_alter_table_is_denied(app_conn):
    # A non-owner gets SQLSTATE 42501 with "must be owner of table", not "permission denied".
    for table in TABLES:
        statement = sql.SQL("ALTER TABLE {} ADD COLUMN probe integer").format(
            sql.Identifier("public", table)
        )
        with rejected(app_conn, errors.InsufficientPrivilege):
            app_conn.execute(statement)


def test_app_can_lock_a_return_row_with_for_update(app_conn):
    # Optional extra: tests the note that FOR UPDATE needs an UPDATE privilege on one column, which
    # the app has on returns but not on decisions.
    return_id = insert_return(app_conn)
    insert_decision(app_conn, return_id)
    locked = app_conn.execute(
        "SELECT id FROM public.returns WHERE id = %s FOR UPDATE", (return_id,)
    )
    assert locked.fetchone() == (return_id,)
    with rejected(app_conn, errors.InsufficientPrivilege):
        app_conn.execute(
            "SELECT return_id FROM public.decisions WHERE return_id = %s FOR UPDATE", (return_id,)
        )


# ---------- G4: every CHECK constraint has a rejecting test ----------


def test_every_check_constraint_is_exercised(app_conn):
    for table in TABLES:
        require_relation(app_conn, table)
    in_database = {
        name
        for (name,) in app_conn.execute(
            "SELECT conname FROM pg_constraint"
            " WHERE contype = 'c' AND conrelid = ANY(%s::regclass[])",
            ([f"public.{table}" for table in TABLES],),
        ).fetchall()
    }
    listed = set(EXERCISED_CHECKS)
    assert in_database - listed == set(), "CHECK constraints without a test in EXERCISED_CHECKS"
    assert listed - in_database == set(), (
        "names in EXERCISED_CHECKS that the database does not have"
    )
    # Each listed name must also appear as a literal in another test file of this folder, which is
    # where its rejecting test lives (this file is left out, or the list itself would match).
    others = [
        path.read_text(encoding="utf-8")
        for path in sorted(Path(__file__).parent.glob("test_*.py"))
        if path.name != Path(__file__).name
    ]
    unused = [name for name in EXERCISED_CHECKS if not any(f'"{name}"' in text for text in others)]
    assert unused == [], "listed CHECK names with no literal use in another test file"
