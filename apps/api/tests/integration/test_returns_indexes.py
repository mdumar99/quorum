"""The two unique indexes of `returns` (#44, criteria S3 and S4).

S3: the active-return index on (order_id, order_line_id), with the predicate of ADR-0004 D6
(R4 is this index). S4: the idempotency index on (idempotency_key, key_generation). Every test runs
as quorum_app through app_conn, in a transaction that is rolled back.
"""

import pytest
from psycopg import errors
from schema_helpers import insert_return, rejected, require_relation, unique

pytestmark = pytest.mark.integration

ACTIVE_INDEX = "returns_active_order_line_uq"
KEY_INDEX = "returns_idempotency_key_generation_uq"

# Rows that hold the order line's slot: the predicate of ADR-0004 D6.
ACTIVE_ROWS = {
    "processing": {"status": "processing"},
    "pending-review": {"status": "pending_review", "outcome": "escalate"},
    "decided-approve": {"status": "decided", "outcome": "approve"},
}
# Rows that do not: a failed return and a denied one.
FREE_ROWS = {
    "failed": {"status": "failed", "error_type": "llm_timeout", "retry_after_seconds": 30},
    "decided-deny": {"status": "decided", "outcome": "deny"},
}


# ---------- S3: the active-return index ----------


@pytest.mark.parametrize("holder", ACTIVE_ROWS.values(), ids=ACTIVE_ROWS.keys())
def test_second_active_return_for_an_order_line_is_rejected_with_23505(app_conn, holder):
    order_id, line_id = unique("order"), unique("line")
    insert_return(app_conn, order_id=order_id, order_line_id=line_id, **holder)
    with pytest.raises(errors.UniqueViolation) as caught:
        with app_conn.transaction():
            insert_return(app_conn, order_id=order_id, order_line_id=line_id, status="processing")
    assert caught.value.sqlstate == "23505"
    assert caught.value.diag.constraint_name == ACTIVE_INDEX


@pytest.mark.parametrize("holder", FREE_ROWS.values(), ids=FREE_ROWS.keys())
def test_failed_and_decided_deny_do_not_hold_the_slot(app_conn, holder):
    order_id, line_id = unique("order"), unique("line")
    insert_return(app_conn, order_id=order_id, order_line_id=line_id, **holder)
    insert_return(app_conn, order_id=order_id, order_line_id=line_id, status="processing")


def test_failed_to_processing_update_can_hit_the_index(app_conn):
    order_id, line_id = unique("order"), unique("line")
    insert_return(app_conn, order_id=order_id, order_line_id=line_id, status="processing")
    failed_id = insert_return(
        app_conn, order_id=order_id, order_line_id=line_id, **FREE_ROWS["failed"]
    )
    with rejected(app_conn, errors.UniqueViolation, ACTIVE_INDEX):
        app_conn.execute(
            "UPDATE public.returns SET status = 'processing', attempt = 2,"
            " error_type = NULL, retry_after_seconds = NULL WHERE id = %s",
            (failed_id,),
        )


def test_same_order_line_id_in_a_different_order_is_not_a_collision(app_conn):
    line_id = unique("line")
    insert_return(app_conn, order_id=unique("order"), order_line_id=line_id)
    insert_return(app_conn, order_id=unique("order"), order_line_id=line_id)


# ---------- S4: the idempotency-key index ----------


def test_duplicate_key_and_generation_is_rejected(app_conn):
    key = unique("key")
    insert_return(app_conn, idempotency_key=key, key_generation=1)
    with rejected(app_conn, errors.UniqueViolation, KEY_INDEX):
        insert_return(app_conn, idempotency_key=key, key_generation=1)


def test_next_generation_of_the_same_key_is_allowed(app_conn):
    key = unique("key")
    insert_return(app_conn, idempotency_key=key, key_generation=1)
    insert_return(app_conn, idempotency_key=key, key_generation=2)


def test_the_two_unique_indexes_have_distinct_stable_names(app_conn):
    require_relation(app_conn, "returns")
    names = {
        name
        for (name,) in app_conn.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = 'public' AND tablename = 'returns'"
            " AND indexdef LIKE 'CREATE UNIQUE INDEX%'"
        ).fetchall()
    }
    assert {ACTIVE_INDEX, KEY_INDEX} <= names
    assert ACTIVE_INDEX != KEY_INDEX


def test_23505_constraint_name_tells_the_two_indexes_apart(app_conn):
    key, order_id, line_id = unique("key"), unique("order"), unique("line")
    insert_return(app_conn, idempotency_key=key, order_id=order_id, order_line_id=line_id)
    with pytest.raises(errors.UniqueViolation) as by_key:
        with app_conn.transaction():
            insert_return(app_conn, idempotency_key=key)  # same key and generation, other line
    with pytest.raises(errors.UniqueViolation) as by_line:
        with app_conn.transaction():
            insert_return(app_conn, order_id=order_id, order_line_id=line_id)  # same active line
    assert by_key.value.diag.constraint_name == KEY_INDEX
    assert by_line.value.diag.constraint_name == ACTIVE_INDEX
