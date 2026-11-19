"""Criterion 1: `insert_return` stores a `processing` return (issue #54; ADR-0004, Decisions 2, 5).

Each test runs the function as quorum_app on its own connection; the owner only reads the result
back. The rows carry the run marker, and the owner removes them afterwards.
"""

import inspect

import pytest
from repo_data import (
    HASH_A,
    PV,
    audit_rows,
    count_by_key,
    decision_rows,
    fields_for,
    key_for,
    owner_row,
    run,
    with_conn,
)

from app.db.models import Inserted
from app.db.returns import insert_return

pytestmark = pytest.mark.integration


async def insert(url, fields, key, *, generation=1, policy_version=PV, commit=True):
    async def action(conn):
        return await insert_return(
            conn,
            fields,
            idempotency_key=key,
            payload_hash=HASH_A,
            key_generation=generation,
            policy_version=policy_version,
        )

    return await with_conn(url, action, commit=commit)


def test_it_inserts_a_processing_return_with_attempt_1(marker, owner_conn, app_url):
    fields = fields_for(marker)
    key = key_for(marker)
    result = run(insert(app_url, fields, key))
    assert isinstance(result, Inserted)
    stored = owner_row(owner_conn, result.row.id)
    assert stored["status"] == "processing"
    assert stored["outcome"] is None
    assert stored["error_type"] is None
    assert stored["retry_after_seconds"] is None
    assert stored["attempt"] == 1


def test_it_stores_the_seven_fields_the_key_the_hash_the_generation_and_the_policy_version(
    marker, owner_conn, app_url
):
    fields = fields_for(marker, line="7", amount_minor=12345, category="books")
    key = key_for(marker, "stored")
    result = run(insert(app_url, fields, key, generation=2))
    stored = owner_row(owner_conn, result.row.id)
    for name, value in fields.model_dump().items():
        assert stored[name] == value, name
    assert stored["idempotency_key"] == key
    assert stored["payload_hash"] == HASH_A
    assert stored["key_generation"] == 2
    assert stored["policy_version"] == PV


@pytest.mark.parametrize("generation", [1, 3])
def test_the_key_generation_given_is_the_one_stored(marker, owner_conn, app_url, generation):
    result = run(insert(app_url, fields_for(marker), key_for(marker), generation=generation))
    assert owner_row(owner_conn, result.row.id)["key_generation"] == generation


def test_the_text_is_stored_verbatim(marker, owner_conn, app_url):
    reason = "  café 日本語 ☃ \n\ttrailing  "
    result = run(insert(app_url, fields_for(marker, reason=reason), key_for(marker)))
    assert owner_row(owner_conn, result.row.id)["reason"] == reason


def test_the_policy_version_given_is_pinned_as_it_is(marker, owner_conn, app_url):
    other = "v2:" + "b" * 64
    result = run(insert(app_url, fields_for(marker), key_for(marker), policy_version=other))
    assert owner_row(owner_conn, result.row.id)["policy_version"] == other


def test_the_stored_row_is_returned(marker, owner_conn, app_url):
    result = run(insert(app_url, fields_for(marker), key_for(marker)))
    stored = owner_row(owner_conn, result.row.id)
    assert result.row.model_dump() == stored


def test_as_of_is_the_database_time_of_the_transaction_and_is_never_supplied(
    marker, owner_conn, app_url
):
    assert "as_of" not in inspect.signature(insert_return).parameters
    seen = {}

    async def action(conn):
        cursor = await conn.execute("SELECT now()")
        (seen["now"],) = await cursor.fetchone()
        return await insert_return(
            conn,
            fields_for(marker),
            idempotency_key=key_for(marker),
            payload_hash=HASH_A,
            key_generation=1,
            policy_version=PV,
        )

    result = run(with_conn(app_url, action))
    stored = owner_row(owner_conn, result.row.id)
    # now() is the start of the transaction, so the insert and the SELECT before it agree exactly.
    assert stored["as_of"] == seen["now"]
    assert stored["created_at"] == seen["now"]
    assert stored["updated_at"] == seen["now"]


def test_a_plain_insert_writes_no_decision_and_no_audit_row(marker, owner_conn, app_url):
    result = run(insert(app_url, fields_for(marker), key_for(marker)))
    assert decision_rows(owner_conn, result.row.id) == []
    assert audit_rows(owner_conn, result.row.id) == []


# The call under test must be the FIRST thing on its connection: no statement may run before it.
# psycopg treats `transaction()` on an idle connection as an OUTER transaction and commits it on
# exit, so a savepoint opened on an idle connection would commit here even though the caller did
# not. A statement run first (a SELECT, say) would make the connection busy and hide exactly that.
def test_the_function_does_not_commit_the_caller_owns_the_transaction(marker, owner_conn, app_url):
    result = run(insert(app_url, fields_for(marker), key_for(marker), commit=False))
    assert isinstance(result, Inserted)
    assert count_by_key(owner_conn, key_for(marker)) == 0  # rolled back with the connection


def test_two_different_order_lines_both_insert(marker, owner_conn, app_url):
    first = run(insert(app_url, fields_for(marker, line="1"), key_for(marker, "1")))
    second = run(insert(app_url, fields_for(marker, line="2"), key_for(marker, "2")))
    assert isinstance(first, Inserted)
    assert isinstance(second, Inserted)
    assert first.row.id != second.row.id
