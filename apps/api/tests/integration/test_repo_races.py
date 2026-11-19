"""Criterion 9: two-connection races, made deterministic (issue #54).

No test here hopes that two tasks collide. Connection A does its statement and keeps its
transaction OPEN; connection B starts the competing statement as an asyncio task; the test waits
until `pg_stat_activity` shows B waiting on a lock (`wait_until_blocked`); only then A commits (or
rolls back) and B is allowed to finish. If B never blocks, the test fails: a race that was not
exercised is not a pass. Both connections are quorum_app. The owner is used for setup, for reading
the result back and, in one test, to play the takeover that P1-13 will add.
"""

import asyncio

import psycopg
import pytest
from repo_data import (
    HASH_A,
    HASH_B,
    PV,
    R4_ARRAY,
    app_connection,
    audit_rows,
    count_by_key,
    decision_rows,
    fields_for,
    key_for,
    llm_decision,
    new_return,
    owner_row,
    run,
    wait_until_blocked,
)

from app.db.models import Applied, Inserted, KeyConflict, Lost, R4Denied, Retried
from app.db.returns import (
    finalize_decided,
    finalize_failed,
    get_return,
    insert_return,
    retry_failed,
)

pytestmark = pytest.mark.integration


def insert_call(conn, fields, key, payload_hash=HASH_A):
    return insert_return(
        conn,
        fields,
        idempotency_key=key,
        payload_hash=payload_hash,
        key_generation=1,
        policy_version=PV,
    )


# ---------- inserts ----------


@pytest.mark.parametrize("line_b", ["2", "1"], ids=["other-order-line", "same-order-line"])
def test_two_same_key_inserts_one_wins_and_the_other_gets_a_key_conflict(
    marker, owner_conn, owner_database_url, app_url, line_b
):
    # On the same order line BOTH unique indexes are violated by the waiting insert; the answer
    # must still be a key conflict, not R4 (a same-key request is a replay, not a duplicate).
    async def scenario():
        async with app_connection(app_url) as a, app_connection(app_url) as b:
            first = await insert_call(a, fields_for(marker, "1"), key_for(marker))
            task = asyncio.create_task(insert_call(b, fields_for(marker, line_b), key_for(marker)))
            await wait_until_blocked(owner_database_url, b.info.backend_pid)
            assert not task.done()
            await a.commit()
            second = await task
            await b.commit()
            return first, second

    first, second = run(scenario())
    assert isinstance(first, Inserted)
    assert second == KeyConflict()
    assert count_by_key(owner_conn, key_for(marker)) == 1
    assert audit_rows(owner_conn, first.row.id) == []
    assert decision_rows(owner_conn, first.row.id) == []


def test_two_different_keys_on_one_order_line_one_processing_and_one_r4(
    marker, owner_conn, owner_database_url, app_url
):
    async def scenario():
        async with app_connection(app_url) as a, app_connection(app_url) as b:
            first = await insert_call(a, fields_for(marker), key_for(marker, "a"))
            task = asyncio.create_task(
                insert_call(b, fields_for(marker), key_for(marker, "b"), HASH_B)
            )
            await wait_until_blocked(owner_database_url, b.info.backend_pid)
            assert not task.done()
            await a.commit()
            second = await task
            await b.commit()
            return first, second

    first, second = run(scenario())
    assert isinstance(first, Inserted)
    assert isinstance(second, R4Denied)
    winner = owner_row(owner_conn, first.row.id)
    loser = owner_row(owner_conn, second.row.id)
    assert (winner["status"], winner["outcome"]) == ("processing", None)
    assert (loser["status"], loser["outcome"]) == ("decided", "deny")
    audits = audit_rows(owner_conn, second.row.id)
    assert [(a["event_type"], a["attempt"]) for a in audits] == [("r4_denied", 1)]
    assert audits[0]["conflicting_return_id"] == first.row.id
    (decision,) = decision_rows(owner_conn, second.row.id)
    assert decision["rule_results"] == R4_ARRAY
    assert audit_rows(owner_conn, first.row.id) == []


def test_when_the_first_transaction_rolls_back_the_waiting_insert_wins(
    marker, owner_conn, owner_database_url, app_url
):
    async def scenario():
        async with app_connection(app_url) as a, app_connection(app_url) as b:
            await insert_call(a, fields_for(marker), key_for(marker, "a"))
            task = asyncio.create_task(
                insert_call(b, fields_for(marker), key_for(marker, "b"), HASH_B)
            )
            await wait_until_blocked(owner_database_url, b.info.backend_pid)
            await a.rollback()
            second = await task
            await b.commit()
            return second

    second = run(scenario())
    assert isinstance(second, Inserted)
    assert owner_row(owner_conn, second.row.id)["status"] == "processing"
    assert count_by_key(owner_conn, key_for(marker, "a")) == 0


# ---------- retries ----------


def test_two_concurrent_retries_of_one_failed_row_one_is_retried_and_one_is_lost(
    marker, owner_conn, owner_database_url, app_url
):
    return_id = new_return(
        owner_conn, marker, status="failed", error_type="llm_timeout", retry_after_seconds=5
    )

    async def scenario():
        async with app_connection(app_url) as a, app_connection(app_url) as b:
            row_a = await get_return(a, return_id)
            row_b = await get_return(b, return_id)  # both saw the row as failed, attempt 1
            first = await retry_failed(a, row_a)  # holds the row lock, uncommitted
            task = asyncio.create_task(retry_failed(b, row_b))
            await wait_until_blocked(owner_database_url, b.info.backend_pid)
            assert not task.done()
            await a.commit()
            second = await task
            await b.commit()
            return first, second

    first, second = run(scenario())
    assert isinstance(first, Retried)
    assert second == Lost()
    stored = owner_row(owner_conn, return_id)
    assert (stored["status"], stored["attempt"]) == ("processing", 2)  # moved once, not twice
    assert audit_rows(owner_conn, return_id) == []  # a lost retry never was an attempt


# ---------- finalizes ----------


def test_a_late_finalize_after_a_takeover_is_lost_and_the_row_stays_with_the_new_attempt(
    marker, owner_conn, owner_database_url, app_url
):
    # P1-13 will add the takeover. Here the owner plays it: processing -> failed (abandoned) ->
    # processing with attempt 2, in one open transaction, while the original attempt 1 tries to
    # finalize. The late finalize must wait for that transaction and then find attempt 2: Lost.
    return_id = new_return(owner_conn, marker, status="processing", attempt=1)

    async def scenario():
        with psycopg.connect(owner_database_url) as takeover:
            takeover.execute(
                "UPDATE public.returns SET status = 'failed', error_type = 'abandoned' "
                "WHERE id = %s",
                (return_id,),
            )
            takeover.execute(
                "UPDATE public.returns SET status = 'processing', error_type = NULL, "
                "attempt = 2 WHERE id = %s",
                (return_id,),
            )
            async with app_connection(app_url) as late:
                task = asyncio.create_task(
                    finalize_decided(
                        late, return_id=return_id, attempt=1, decision=llm_decision("approve")
                    )
                )
                await wait_until_blocked(owner_database_url, late.info.backend_pid)
                assert not task.done()
                takeover.commit()
                result = await task
                await late.commit()
                return result

    assert run(scenario()) == Lost()
    stored = owner_row(owner_conn, return_id)
    assert (stored["status"], stored["outcome"], stored["attempt"]) == ("processing", None, 2)
    assert decision_rows(owner_conn, return_id) == []
    audits = audit_rows(owner_conn, return_id)
    assert [(a["event_type"], a["attempt"]) for a in audits] == [("superseded", 1)]


def test_two_finalizes_of_the_same_attempt_only_one_applies(
    marker, owner_conn, owner_database_url, app_url
):
    return_id = new_return(owner_conn, marker, status="processing", attempt=1)

    async def scenario():
        async with app_connection(app_url) as a, app_connection(app_url) as b:
            first = await finalize_decided(
                a, return_id=return_id, attempt=1, decision=llm_decision("deny")
            )
            task = asyncio.create_task(
                finalize_failed(
                    b,
                    return_id=return_id,
                    attempt=1,
                    error_type="llm_timeout",
                    retry_after_seconds=5,
                )
            )
            await wait_until_blocked(owner_database_url, b.info.backend_pid)
            assert not task.done()
            await a.commit()
            second = await task
            await b.commit()
            return first, second

    first, second = run(scenario())
    assert isinstance(first, Applied)
    assert second == Lost()
    stored = owner_row(owner_conn, return_id)
    assert (stored["status"], stored["outcome"]) == ("decided", "deny")
    events = [a["event_type"] for a in audit_rows(owner_conn, return_id)]
    assert sorted(events) == ["llm_decided", "superseded"]
    assert len(decision_rows(owner_conn, return_id)) == 1
