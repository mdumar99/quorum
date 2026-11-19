"""The matrix: what "active" means, checked against the real unique index (issue #54; ADR-0004, D6).

`ACTIVE_RETURN_PREDICATE_SQL` is a boolean SQL expression over the columns of `public.returns`. The
repository uses it to find the return that blocks an insert. It must agree with the partial unique
index `returns_active_order_line_uq`, which is the authority. For every combination of status and
outcome the owner inserts a first return, and then:
- the predicate selects the row exactly when a second insert on the same order line collides;
- that collision is reported by the index named in `ACTIVE_ORDER_LINE_INDEX`;
- `insert_return` gives R4 exactly then, and a plain insert otherwise.
A different order line never collides, whatever the status. The cases are literal.
"""

import psycopg
import pytest
from repo_data import (
    HASH_B,
    PV,
    fields_for,
    key_for,
    new_return,
    order_for,
    run,
    with_conn,
)
from schema_helpers import insert_return as owner_insert

from app.db.models import Inserted, R4Denied
from app.db.returns import ACTIVE_ORDER_LINE_INDEX, ACTIVE_RETURN_PREDICATE_SQL, insert_return

pytestmark = pytest.mark.integration

# (the columns of the first return, whether it is "active")
COMBINATIONS = [
    pytest.param({"status": "processing"}, True, id="processing"),
    pytest.param({"status": "pending_review", "outcome": "escalate"}, True, id="pending_review"),
    pytest.param({"status": "decided", "outcome": "approve"}, True, id="decided-approve"),
    pytest.param({"status": "decided", "outcome": "deny"}, False, id="decided-deny"),
    pytest.param({"status": "failed", "error_type": "llm_timeout"}, False, id="failed"),
    pytest.param(
        {"status": "failed", "error_type": "invalid_llm_output"}, False, id="failed-permanent"
    ),
]


def _second_insert_collides(owner_conn, marker, line):
    """Try a second `processing` insert on the order line; roll everything back afterwards."""
    try:
        with owner_conn.transaction():
            owner_insert(
                owner_conn,
                order_id=order_for(marker),
                order_line_id=f"line-{line}",
                idempotency_key=key_for(marker, "second"),
                payload_hash=HASH_B,
                policy_version=PV,
                currency="USD",
            )
    except psycopg.errors.UniqueViolation as violation:
        return True, violation.diag.constraint_name
    finally:
        owner_conn.rollback()  # nothing of the second insert survives
    return False, None


@pytest.mark.parametrize(("columns", "active"), COMBINATIONS)
def test_the_predicate_selects_a_row_exactly_when_a_second_insert_collides(
    marker, owner_conn, columns, active
):
    assert ACTIVE_RETURN_PREDICATE_SQL.strip(), "the predicate is empty"
    first = new_return(owner_conn, marker, name="first", **columns)
    selected = owner_conn.execute(
        f"SELECT count(*) FROM public.returns WHERE id = %s AND ({ACTIVE_RETURN_PREDICATE_SQL})",
        (first,),
    ).fetchone()[0]
    owner_conn.rollback()
    collided, constraint = _second_insert_collides(owner_conn, marker, line="1")
    assert collided is active  # the index says what "active" is
    assert (selected == 1) is active  # and the predicate says the same
    if active:
        assert constraint == ACTIVE_ORDER_LINE_INDEX


@pytest.mark.parametrize(("columns", "active"), COMBINATIONS)
def test_insert_return_gives_r4_exactly_for_an_active_blocker(
    marker, owner_conn, app_url, columns, active
):
    new_return(owner_conn, marker, name="first", **columns)

    async def action(conn):
        return await insert_return(
            conn,
            fields_for(marker),
            idempotency_key=key_for(marker, "second"),
            payload_hash=HASH_B,
            key_generation=1,
            policy_version=PV,
        )

    result = run(with_conn(app_url, action))
    assert isinstance(result, R4Denied if active else Inserted)


@pytest.mark.parametrize(("columns", "active"), COMBINATIONS)
def test_a_different_order_line_never_collides(marker, owner_conn, columns, active):
    new_return(owner_conn, marker, name="first", line="1", **columns)
    collided, _ = _second_insert_collides(owner_conn, marker, line="2")
    assert collided is False
