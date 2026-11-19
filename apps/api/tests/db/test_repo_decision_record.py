"""The invariants of `DecisionRecord` (issue #54, criterion 6; ADR-0003, Decision 10).

1. The model call is present if and only if `decided_by` is "llm" (the schema's
   `decisions_llm_fields_ck` says the same about the ten LLM columns).
2. The outcome is one of the policy result's `allowed_outcomes` (`decisions_outcome_ck`).
A violation is a pydantic ValidationError, raised when the record is built, so a bad record never
reaches the database. The PolicyResults are real ones from the real v1 policy.
"""

import pytest
from pydantic import ValidationError
from repo_unit_helpers import llm_record, policy_result

from app.db.models import DecisionRecord

OUTCOMES = ["approve", "deny", "escalate"]


def test_the_fixtures_are_what_the_tests_assume():
    assert policy_result("shoes").allowed_outcomes == ("approve", "deny", "escalate")
    assert policy_result("digital").allowed_outcomes == ("deny",)


# ---------- llm iff decided_by == "llm" ----------


def test_a_rules_decision_without_a_model_call_is_valid():
    record = DecisionRecord(outcome="deny", decided_by="rules", policy_result=policy_result())
    assert record.llm is None


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_an_llm_decision_with_a_model_call_is_valid(outcome):
    record = DecisionRecord(
        outcome=outcome, decided_by="llm", policy_result=policy_result(), llm=llm_record()
    )
    assert record.llm == llm_record()


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_an_llm_decision_without_a_model_call_is_refused(outcome):
    with pytest.raises(ValidationError):
        DecisionRecord(outcome=outcome, decided_by="llm", policy_result=policy_result())


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_a_rules_decision_with_a_model_call_is_refused(outcome):
    with pytest.raises(ValidationError):
        DecisionRecord(
            outcome=outcome, decided_by="rules", policy_result=policy_result(), llm=llm_record()
        )


# ---------- the outcome is one of the allowed outcomes ----------


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_outcome_is_valid_when_the_policy_allows_all_three(outcome):
    DecisionRecord(
        outcome=outcome, decided_by="llm", policy_result=policy_result("shoes"), llm=llm_record()
    )


def test_deny_is_valid_after_a_hard_deny():
    DecisionRecord(outcome="deny", decided_by="rules", policy_result=policy_result("digital"))


@pytest.mark.parametrize("outcome", ["approve", "escalate"])
def test_an_outcome_outside_allowed_outcomes_is_refused(outcome):
    # `digital` is excluded by R2, so only deny is allowed.
    with pytest.raises(ValidationError):
        DecisionRecord(outcome=outcome, decided_by="rules", policy_result=policy_result("digital"))


def test_an_unknown_outcome_or_decider_is_refused():
    with pytest.raises(ValidationError):
        DecisionRecord(outcome="refund", decided_by="rules", policy_result=policy_result())
    with pytest.raises(ValidationError):
        DecisionRecord(outcome="deny", decided_by="human", policy_result=policy_result())
