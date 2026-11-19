"""Criteria 6 and 7: the decision and audit rows of a finalize (issue #54; ADR-0003, Decision 10).

A finalize does the compare-and-set first. Applied: the decision row (only for `decided` and
`pending_review`) and the audit row are written in the same transaction. Lost: only the audit row
(`superseded`, the loser's attempt), and no decision. A failure writes `failed_<error_type>` and no
decision. A rollback after a finalize leaves neither the status nor the rows. One audit row per
attempt; every one of the eleven event types can be inserted.
"""

import psycopg
import pytest
from repo_data import (
    ALL_EVENT_TYPES,
    LLM_COLUMNS,
    SIX_ERROR_TYPES,
    audit_rows,
    backdate_updated,
    decision_rows,
    llm_call,
    llm_decision,
    new_return,
    owner_row,
    policy_result,
    rules_decision,
    run,
    with_conn,
)

from app.db.audit import insert_audit
from app.db.models import Applied, DecisionRecord
from app.db.returns import finalize_decided, finalize_failed, get_return, retry_failed

pytestmark = pytest.mark.integration

NO_LLM = dict.fromkeys(LLM_COLUMNS)


def llm_columns(row: dict) -> dict:
    return {column: row[column] for column in LLM_COLUMNS}


def decide(url, return_id, attempt, decision):
    return run(
        with_conn(
            url,
            lambda c: finalize_decided(c, return_id=return_id, attempt=attempt, decision=decision),
        )
    )


def fail(url, return_id, attempt, error_type, llm=None, retry_after=None):
    return run(
        with_conn(
            url,
            lambda c: finalize_failed(
                c,
                return_id=return_id,
                attempt=attempt,
                error_type=error_type,
                retry_after_seconds=retry_after,
                llm=llm,
            ),
        )
    )


# ---------- Applied: the decision row and the audit row ----------


def test_a_rules_decision_writes_its_decision_row_and_its_audit_row(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing", attempt=2)
    decision = rules_decision("deny")
    decide(app_url, return_id, 2, decision)
    decisions = decision_rows(owner_conn, return_id)
    assert len(decisions) == 1
    row = decisions[0]
    assert row["policy_version"] == owner_row(owner_conn, return_id)["policy_version"]
    assert row["policy_version"] == decision.policy_result.policy_version
    assert (row["outcome"], row["decided_by"]) == ("deny", "rules")
    assert row["rule_results"] == decision.policy_result.rule_results_json()
    assert row["allowed_outcomes"] == ["deny"]
    assert llm_columns(row) == NO_LLM
    audits = audit_rows(owner_conn, return_id)
    assert [(a["event_type"], a["attempt"]) for a in audits] == [("rules_decided", 2)]
    assert llm_columns(audits[0]) == NO_LLM
    assert audits[0]["conflicting_return_id"] is None


def test_an_llm_decision_stores_the_call_on_both_rows(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing", attempt=1)
    decision = llm_decision("approve")
    decide(app_url, return_id, 1, decision)
    (row,) = decision_rows(owner_conn, return_id)
    assert (row["outcome"], row["decided_by"]) == ("approve", "llm")
    assert row["allowed_outcomes"] == ["approve", "deny", "escalate"]
    assert llm_columns(row) == decision.llm.columns()
    (audit,) = audit_rows(owner_conn, return_id)
    assert (audit["event_type"], audit["attempt"]) == ("llm_decided", 1)
    assert llm_columns(audit) == decision.llm.columns()


def test_escalate_gives_pending_review_with_a_decision_row(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing")
    decision = llm_decision("escalate")
    decide(app_url, return_id, 1, decision)
    stored = owner_row(owner_conn, return_id)
    assert (stored["status"], stored["outcome"]) == ("pending_review", "escalate")
    (row,) = decision_rows(owner_conn, return_id)
    assert (row["outcome"], row["decided_by"]) == ("escalate", "llm")
    (audit,) = audit_rows(owner_conn, return_id)
    assert audit["event_type"] == "escalated"
    assert llm_columns(audit) == decision.llm.columns()


def test_an_escalate_is_escalated_whoever_decided(marker, owner_conn, app_url):
    # No pipeline path builds this: a hard deny allows only deny, and when R3 limits the outcomes
    # the MODEL decides. The function's contract is that an escalate is recorded as `escalated`
    # (and the return as pending_review) whoever decided; here the policy allows all three outcomes.
    return_id = new_return(owner_conn, marker, status="processing")
    decision = DecisionRecord(
        outcome="escalate", decided_by="rules", policy_result=policy_result("shoes")
    )
    decide(app_url, return_id, 1, decision)
    stored = owner_row(owner_conn, return_id)
    assert (stored["status"], stored["outcome"]) == ("pending_review", "escalate")
    decisions = decision_rows(owner_conn, return_id)
    assert len(decisions) == 1
    assert (decisions[0]["outcome"], decisions[0]["decided_by"]) == ("escalate", "rules")
    assert llm_columns(decisions[0]) == NO_LLM
    audits = audit_rows(owner_conn, return_id)
    assert [(a["event_type"], a["attempt"]) for a in audits] == [("escalated", 1)]


# ---------- a failure: no decision, an audit row with the error ----------


@pytest.mark.parametrize("error_type", SIX_ERROR_TYPES)
def test_a_failure_writes_no_decision_and_an_audit_row_with_the_call(
    marker, owner_conn, app_url, error_type
):
    return_id = new_return(owner_conn, marker, status="processing", attempt=4)
    call = llm_call("not json at all")
    fail(app_url, return_id, 4, error_type, llm=call)
    assert decision_rows(owner_conn, return_id) == []
    (audit,) = audit_rows(owner_conn, return_id)
    assert (audit["event_type"], audit["attempt"]) == (f"failed_{error_type}", 4)
    assert llm_columns(audit) == call.columns()  # the raw output is kept, valid or not


def test_a_failure_without_a_model_call_has_empty_llm_columns(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing")
    fail(app_url, return_id, 1, "capacity")  # the semaphore was full: no call was made
    (audit,) = audit_rows(owner_conn, return_id)
    assert audit["event_type"] == "failed_capacity"
    assert llm_columns(audit) == NO_LLM
    assert decision_rows(owner_conn, return_id) == []  # a failure never writes a decision


# ---------- Lost: the audit row only ----------


def test_a_lost_decision_writes_only_a_superseded_audit_row_of_the_losers_attempt(
    marker, owner_conn, app_url
):
    return_id = new_return(owner_conn, marker, status="processing", attempt=2)
    backdate_updated(owner_conn, return_id, 3600)
    before = owner_row(owner_conn, return_id)
    decision = llm_decision("approve")
    decide(app_url, return_id, 1, decision)  # attempt 1 is stale: the row is on attempt 2
    assert owner_row(owner_conn, return_id) == before
    assert decision_rows(owner_conn, return_id) == []
    (audit,) = audit_rows(owner_conn, return_id)
    assert (audit["event_type"], audit["attempt"]) == ("superseded", 1)
    assert llm_columns(audit) == decision.llm.columns()


def test_a_lost_failure_writes_superseded_not_the_failed_event(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing", attempt=2)
    call = llm_call()
    fail(app_url, return_id, 1, "llm_timeout", llm=call)
    assert owner_row(owner_conn, return_id)["status"] == "processing"
    (audit,) = audit_rows(owner_conn, return_id)
    assert (audit["event_type"], audit["attempt"]) == ("superseded", 1)
    assert llm_columns(audit) == call.columns()
    assert decision_rows(owner_conn, return_id) == []  # a failure never writes a decision


# ---------- one audit row per attempt ----------


def test_there_is_one_audit_row_per_attempt_across_a_retry(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing", attempt=1)
    fail(app_url, return_id, 1, "llm_timeout", retry_after=5)

    async def retry(conn):
        return await retry_failed(conn, await get_return(conn, return_id))

    run(with_conn(app_url, retry))
    # Entering `processing` again writes nothing: attempt 2 gets its audit row when it finalizes.
    assert [(a["attempt"], a["event_type"]) for a in audit_rows(owner_conn, return_id)] == [
        (1, "failed_llm_timeout")
    ]
    decide(app_url, return_id, 2, llm_decision("deny"))
    assert [(a["attempt"], a["event_type"]) for a in audit_rows(owner_conn, return_id)] == [
        (1, "failed_llm_timeout"),
        (2, "llm_decided"),
    ]


# ---------- the transaction ----------


def test_a_rollback_after_a_finalize_leaves_neither_the_status_nor_the_rows(
    marker, owner_conn, app_url
):
    return_id = new_return(owner_conn, marker, status="processing")
    before = owner_row(owner_conn, return_id)
    result = run(
        with_conn(
            app_url,
            lambda c: finalize_decided(c, return_id=return_id, attempt=1, decision=llm_decision()),
            commit=False,  # the connection is rolled back instead
        )
    )
    assert isinstance(result, Applied)
    assert owner_row(owner_conn, return_id) == before
    assert decision_rows(owner_conn, return_id) == []
    assert audit_rows(owner_conn, return_id) == []


def test_a_decision_with_another_policy_version_than_the_returns_is_refused(
    marker, owner_conn, app_url
):
    return_id = new_return(owner_conn, marker, status="processing")
    other = policy_result().model_copy(update={"policy_version": "v9:" + "c" * 64})
    decision = DecisionRecord(
        outcome="approve", decided_by="llm", policy_result=other, llm=llm_call()
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        decide(app_url, return_id, 1, decision)
    # The composite foreign key (id, policy_version) enforces "equal to the return's", and the
    # whole transaction, the status change included, was rolled back with the error.
    assert owner_row(owner_conn, return_id)["status"] == "processing"
    assert decision_rows(owner_conn, return_id) == []


# ---------- criterion 7: the audit row ----------


@pytest.mark.parametrize("event_type", ALL_EVENT_TYPES)
def test_every_one_of_the_eleven_event_types_inserts(marker, owner_conn, app_url, event_type):
    return_id = new_return(owner_conn, marker, status="processing")
    audit_id = run(with_conn(app_url, lambda c: insert_audit(c, return_id, 3, event_type)))
    assert isinstance(audit_id, int)
    rows = audit_rows(owner_conn, return_id)
    assert len(rows) == 1, f"expected one audit row for {event_type!r}, found {len(rows)}"
    (audit,) = rows
    assert audit["id"] == audit_id
    assert (audit["event_type"], audit["attempt"]) == (event_type, 3)


def test_the_audit_row_carries_the_llm_columns_and_the_conflicting_return(
    marker, owner_conn, app_url
):
    first = new_return(owner_conn, marker, name="a", status="processing")
    second = new_return(owner_conn, marker, name="b", status="failed", error_type="capacity")
    call = llm_call()
    run(
        with_conn(
            app_url,
            lambda c: insert_audit(
                c, second, 5, "r4_denied", llm=call, conflicting_return_id=first
            ),
        )
    )
    rows = audit_rows(owner_conn, second)
    assert len(rows) == 1, f"expected one audit row for the r4_denied insert, found {len(rows)}"
    (audit,) = rows
    assert audit["conflicting_return_id"] == first
    assert llm_columns(audit) == call.columns()


def test_an_event_type_outside_the_list_writes_no_row(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing")
    with pytest.raises(ValueError):
        run(with_conn(app_url, lambda c: insert_audit(c, return_id, 1, "attempt_started")))
    assert audit_rows(owner_conn, return_id) == []
