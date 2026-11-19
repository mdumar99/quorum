"""Criterion 5: the guarded transitions (issue #54; ADR-0004, Decisions 2 and 5).

`finalize_decided` and `finalize_failed` change a `processing` row only if the status AND the
attempt match; otherwise they report `Lost` and leave the row as it was. `retry_failed` moves a
`failed` row with a TRANSIENT error back to `processing`, increments `attempt` and clears the error.
Every statement runs as quorum_app, which may update only six columns, so an ungranted column would
fail the test with `permission denied`.

Before each call the owner makes `updated_at` one hour old; after it, `updated_at` must have moved
forward (there is no trigger: the statement sets it).
"""

import pytest
from repo_data import (
    SIX_ERROR_TYPES,
    TRANSIENT_FIVE,
    audit_rows,
    backdate_updated,
    llm_decision,
    new_return,
    owner_row,
    rules_decision,
    run,
    with_conn,
)

from app.db.models import Applied, Lost, Retried
from app.db.returns import finalize_decided, finalize_failed, get_return, retry_failed

pytestmark = pytest.mark.integration

UNTOUCHED = ("id", "order_id", "order_line_id", "as_of", "created_at", "policy_version")


def processing(owner, marker, attempt=3, **columns):
    return_id = new_return(owner, marker, status="processing", attempt=attempt, **columns)
    backdate_updated(owner, return_id, 3600)
    return return_id


def failed_row(owner, marker, error_type="llm_timeout", attempt=2):
    retry = None if error_type == "invalid_llm_output" else 30
    return_id = new_return(
        owner,
        marker,
        status="failed",
        error_type=error_type,
        retry_after_seconds=retry,
        attempt=attempt,
    )
    backdate_updated(owner, return_id, 3600)
    return return_id


# ---------- finalize_decided ----------

DECIDED = [
    pytest.param(llm_decision("approve"), "decided", "approve", id="llm-approve"),
    pytest.param(llm_decision("deny"), "decided", "deny", id="llm-deny"),
    pytest.param(rules_decision("deny"), "decided", "deny", id="rules-deny"),
    pytest.param(llm_decision("escalate"), "pending_review", "escalate", id="llm-escalate"),
]


@pytest.mark.parametrize(("decision", "status", "outcome"), DECIDED)
def test_finalize_decided_applies_on_the_right_status_and_attempt(
    marker, owner_conn, app_url, decision, status, outcome
):
    return_id = processing(owner_conn, marker, attempt=3)
    before = owner_row(owner_conn, return_id)
    result = run(
        with_conn(
            app_url,
            lambda c: finalize_decided(c, return_id=return_id, attempt=3, decision=decision),
        )
    )
    assert isinstance(result, Applied)
    after = owner_row(owner_conn, return_id)
    assert (after["status"], after["outcome"]) == (status, outcome)
    assert after["error_type"] is None
    assert after["retry_after_seconds"] is None
    assert after["attempt"] == 3  # finalizing does not change the attempt
    assert after["updated_at"] > before["updated_at"]
    for name in UNTOUCHED:
        assert after[name] == before[name], name
    assert result.row.model_dump() == after


# ---------- finalize_failed ----------

FAILURES = [
    *[pytest.param(t, None, id=f"{t}-no-retry-after") for t in SIX_ERROR_TYPES],
    *[pytest.param(t, 30, id=f"{t}-30s") for t in TRANSIENT_FIVE],
    pytest.param("rate_limited", 3600, id="rate_limited-3600s"),
]


@pytest.mark.parametrize(("error_type", "retry_after"), FAILURES)
def test_finalize_failed_applies_and_records_the_error(
    marker, owner_conn, app_url, error_type, retry_after
):
    return_id = processing(owner_conn, marker, attempt=3)
    before = owner_row(owner_conn, return_id)
    result = run(
        with_conn(
            app_url,
            lambda c: finalize_failed(
                c,
                return_id=return_id,
                attempt=3,
                error_type=error_type,
                retry_after_seconds=retry_after,
            ),
        )
    )
    assert isinstance(result, Applied)
    after = owner_row(owner_conn, return_id)
    assert after["status"] == "failed"
    assert after["outcome"] is None
    assert after["error_type"] == error_type
    assert after["retry_after_seconds"] == retry_after
    assert after["attempt"] == 3
    assert after["updated_at"] > before["updated_at"]
    for name in UNTOUCHED:
        assert after[name] == before[name], name
    assert result.row.model_dump() == after


# ---------- Lost: a stale attempt or a row that is not processing ----------

LOST_ROWS = [
    pytest.param({"status": "processing", "attempt": 2}, 1, id="stale-attempt"),
    pytest.param({"status": "processing", "attempt": 2}, 3, id="attempt-from-the-future"),
    pytest.param({"status": "decided", "outcome": "approve", "attempt": 1}, 1, id="decided"),
    pytest.param({"status": "failed", "error_type": "llm_timeout", "attempt": 1}, 1, id="failed"),
    pytest.param(
        {"status": "pending_review", "outcome": "escalate", "attempt": 1}, 1, id="pending_review"
    ),
]


@pytest.mark.parametrize(("columns", "attempt"), LOST_ROWS)
def test_finalize_decided_is_lost_and_changes_nothing(
    marker, owner_conn, app_url, columns, attempt
):
    return_id = new_return(owner_conn, marker, **columns)
    backdate_updated(owner_conn, return_id, 3600)
    before = owner_row(owner_conn, return_id)
    result = run(
        with_conn(
            app_url,
            lambda c: finalize_decided(
                c, return_id=return_id, attempt=attempt, decision=llm_decision("approve")
            ),
        )
    )
    assert result == Lost()
    assert owner_row(owner_conn, return_id) == before


@pytest.mark.parametrize(("columns", "attempt"), LOST_ROWS)
def test_finalize_failed_is_lost_and_changes_nothing(marker, owner_conn, app_url, columns, attempt):
    return_id = new_return(owner_conn, marker, **columns)
    backdate_updated(owner_conn, return_id, 3600)
    before = owner_row(owner_conn, return_id)
    result = run(
        with_conn(
            app_url,
            lambda c: finalize_failed(
                c,
                return_id=return_id,
                attempt=attempt,
                error_type="llm_timeout",
                retry_after_seconds=30,
            ),
        )
    )
    assert result == Lost()
    assert owner_row(owner_conn, return_id) == before


# ---------- retry_failed ----------


@pytest.mark.parametrize("error_type", TRANSIENT_FIVE)
def test_retry_failed_moves_a_transient_failure_back_to_processing(
    marker, owner_conn, app_url, error_type
):
    return_id = failed_row(owner_conn, marker, error_type=error_type, attempt=2)
    before = owner_row(owner_conn, return_id)

    async def action(conn):
        return await retry_failed(conn, await get_return(conn, return_id))

    result = run(with_conn(app_url, action))
    assert isinstance(result, Retried)
    after = owner_row(owner_conn, return_id)
    assert after["status"] == "processing"
    assert after["outcome"] is None
    assert after["error_type"] is None  # cleared in the same statement
    assert after["retry_after_seconds"] is None  # cleared in the same statement
    assert after["attempt"] == 3  # n + 1
    assert after["updated_at"] > before["updated_at"]
    for name in (*UNTOUCHED, "idempotency_key", "payload_hash", "key_generation"):
        assert after[name] == before[name], name
    assert result.row.model_dump() == after


def test_retry_failed_never_moves_a_permanent_failure(marker, owner_conn, app_url):
    return_id = failed_row(owner_conn, marker, error_type="invalid_llm_output", attempt=2)
    before = owner_row(owner_conn, return_id)

    async def action(conn):
        return await retry_failed(conn, await get_return(conn, return_id))

    assert run(with_conn(app_url, action)) == Lost()
    assert owner_row(owner_conn, return_id) == before
    assert audit_rows(owner_conn, return_id) == []  # a lost retry writes no audit row


@pytest.mark.parametrize(
    "columns",
    [
        {"status": "processing"},
        {"status": "decided", "outcome": "deny"},
        {"status": "pending_review", "outcome": "escalate"},
    ],
    ids=["processing", "decided", "pending_review"],
)
def test_retry_failed_is_lost_for_a_row_that_is_not_failed(marker, owner_conn, app_url, columns):
    return_id = new_return(owner_conn, marker, **columns)
    backdate_updated(owner_conn, return_id, 3600)
    before = owner_row(owner_conn, return_id)

    async def action(conn):
        return await retry_failed(conn, await get_return(conn, return_id))

    assert run(with_conn(app_url, action)) == Lost()
    assert owner_row(owner_conn, return_id) == before
    assert audit_rows(owner_conn, return_id) == []  # a lost retry writes no audit row


def test_retry_failed_is_lost_when_the_row_was_read_before_another_attempt_moved_it(
    marker, owner_conn, app_url
):
    return_id = failed_row(owner_conn, marker, attempt=2)
    stale = run(with_conn(app_url, lambda c: get_return(c, return_id), commit=False))
    owner_conn.execute("UPDATE public.returns SET attempt = 3 WHERE id = %s", (return_id,))
    owner_conn.commit()
    before = owner_row(owner_conn, return_id)
    assert run(with_conn(app_url, lambda c: retry_failed(c, stale))) == Lost()
    assert owner_row(owner_conn, return_id) == before
    assert audit_rows(owner_conn, return_id) == []  # a lost retry writes no audit row
