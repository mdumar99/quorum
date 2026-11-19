"""`r4_denial`: the PolicyResult stored for an R4 loser (issue #54; ADR-0003, Decision 2).

It is built through the existing `PolicyResult` model, so that model's validators apply: exactly
one entry, R4, a failure, the fixed effect `hard_deny`, `allowed_outcomes` of deny only. Its details
hold no client text: the function takes only the policy version, and the entry's `details` are empty
(R4 has no parameter in the policy file). The JSON stored in `decisions.rule_results` is the
one-entry array documented in docs/policy.md.
"""

import inspect

import pytest
from pydantic import ValidationError
from repo_unit_helpers import POLICY_VERSION

from app.db.returns import r4_denial
from app.policy import PolicyResult

DOCUMENTED_ARRAY = [
    {
        "rule_id": "R4",
        "passed": False,
        "effect": "hard_deny",
        "reason_code": "active_return_exists",
        "details": {},
    }
]


def test_it_is_a_valid_policy_result():
    result = r4_denial(POLICY_VERSION)
    assert isinstance(result, PolicyResult)
    # Validated again from its own dump: every validator of the model applies to this shape.
    assert PolicyResult.model_validate(result.model_dump()) == result


def test_it_has_exactly_one_entry_and_it_is_a_failed_r4_hard_deny():
    entries = r4_denial(POLICY_VERSION).rule_results
    assert len(entries) == 1
    entry = entries[0]
    assert entry.rule_id == "R4"
    assert entry.passed is False
    assert entry.effect == "hard_deny"
    assert entry.reason_code == "active_return_exists"


def test_it_allows_deny_only_and_is_a_hard_deny():
    result = r4_denial(POLICY_VERSION)
    assert result.hard_deny is True
    assert result.allowed_outcomes == ("deny",)


def test_the_details_are_empty():
    assert r4_denial(POLICY_VERSION).rule_results[0].details == {}


def test_the_policy_version_is_the_one_given():
    other = "v2:" + "b" * 64
    assert r4_denial(POLICY_VERSION).policy_version == POLICY_VERSION
    assert r4_denial(other).policy_version == other


def test_the_stored_json_is_the_documented_one_entry_array():
    assert r4_denial(POLICY_VERSION).rule_results_json() == DOCUMENTED_ARRAY


def test_no_client_text_can_get_in_it_takes_only_the_policy_version():
    assert list(inspect.signature(r4_denial).parameters) == ["policy_version"]
    # The whole result, compared exactly: a policy version, a rule id, a code, flags and an outcome,
    # and nothing else, so there is no place for client text.
    assert r4_denial(POLICY_VERSION).model_dump(mode="json") == {
        "policy_version": POLICY_VERSION,
        "hard_deny": True,
        "allowed_outcomes": ["deny"],
        "rule_results": DOCUMENTED_ARRAY,
    }


@pytest.mark.parametrize("bad", ["", "not-a-version", "v1", "v1:abc", "V1:" + "a" * 64])
def test_a_malformed_policy_version_is_refused_by_the_model(bad):
    # The PolicyResult validators apply, so a malformed version cannot be stored.
    with pytest.raises(ValidationError):
        r4_denial(bad)
