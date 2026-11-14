"""The PolicyResult and RuleResult JSON form (#46: AC8).

`decisions.rule_results` holds the array of rule results; the other parts of a PolicyResult have
their own columns. The golden examples are the ones approved on #46 and printed in
docs/policy.md. No database is needed.
"""

import json

import pytest
from policy_examples import (
    EXAMPLE_ALL_PASS,
    EXAMPLE_R1_R2_FAIL,
    R4_ENTRY,
    V1_POLICY_VERSION,
    delivered,
    run,
)
from pydantic import ValidationError

from app.policy import PolicyResult, RuleResult

RESULT_KEYS = {"policy_version", "hard_deny", "allowed_outcomes", "rule_results"}
RULE_KEYS = {"rule_id", "passed", "effect", "reason_code", "details"}


def all_pass(policy):
    return run(policy)


def r1_r2_fail(policy):
    return run(policy, delivered_on=delivered(74), category="digital")


def valid_entry(**overrides) -> dict:
    entry = dict(R4_ENTRY)
    entry.update(overrides)
    return entry


def test_result_carries_rule_results_hard_deny_allowed_outcomes_and_policy_version(policy):
    result = all_pass(policy)
    assert set(PolicyResult.model_fields) == RESULT_KEYS
    assert result.policy_version == V1_POLICY_VERSION
    assert result.hard_deny is False
    assert result.allowed_outcomes == ("approve", "deny", "escalate")
    assert [entry.rule_id for entry in result.rule_results] == ["R1", "R2", "R3"]
    assert set(RuleResult.model_fields) == RULE_KEYS


def test_json_form_matches_the_two_documented_examples(policy):
    assert all_pass(policy).model_dump(mode="json") == EXAMPLE_ALL_PASS
    assert r1_r2_fail(policy).model_dump(mode="json") == EXAMPLE_R1_R2_FAIL


@pytest.mark.parametrize("build", [all_pass, r1_r2_fail], ids=["all-pass", "r1-r2-fail"])
def test_json_form_round_trips(policy, build):
    result = build(policy)
    as_dict = result.model_dump(mode="json")
    assert PolicyResult.model_validate(as_dict) == result
    assert PolicyResult.model_validate_json(json.dumps(as_dict)) == result
    assert PolicyResult.model_validate_json(result.model_dump_json()) == result


def test_rule_results_json_is_a_plain_list_for_jsonb(policy):
    result = r1_r2_fail(policy)
    stored = result.rule_results_json()
    assert stored == EXAMPLE_R1_R2_FAIL["rule_results"]
    assert isinstance(stored, list) and all(type(entry) is dict for entry in stored)
    assert json.loads(json.dumps(stored)) == stored  # plain JSON types only, no dates or tuples
    for entry in stored:
        assert set(entry) == RULE_KEYS
        assert all(type(v) in (int, str, bool, type(None)) for v in entry["details"].values())


# ---------- details cannot carry free text, nesting or floats ----------


def test_details_strings_are_at_most_64_characters():
    assert RuleResult.model_validate(valid_entry(details={"k": "a" * 64})).details == {
        "k": "a" * 64
    }
    with pytest.raises(ValidationError):
        RuleResult.model_validate(valid_entry(details={"k": "a" * 65}))


@pytest.mark.parametrize(
    "key",
    ["Does not fit", "", "Delivered_on", "delivered-on", "1abc", "has space", "x" * 65],
    ids=lambda key: repr(key[:20]) + ("..." if len(key) > 20 else ""),
)
def test_details_keys_follow_the_snake_case_pattern_and_the_64_character_limit(key):
    # The same pattern and limit as reason_code, so free text cannot be smuggled in as a key.
    with pytest.raises(ValidationError):
        RuleResult.model_validate(valid_entry(details={key: 1}))


def test_details_keys_accept_the_real_keys_and_a_64_character_key():
    real_keys = set()
    for example in (EXAMPLE_ALL_PASS, EXAMPLE_R1_R2_FAIL):
        for entry in example["rule_results"]:
            real_keys |= set(entry["details"])
            assert RuleResult.model_validate(entry).details == entry["details"]
    assert real_keys == {
        "delivered_on",
        "as_of_date_utc",
        "days_since_delivery",
        "window_days",
        "category_excluded",
        "matched_category",
        "amount_minor",
        "ceiling_minor",
        "currency",
        "currency_matches",
    }
    assert RuleResult.model_validate(valid_entry(details={"x" * 64: 1})).details == {"x" * 64: 1}


@pytest.mark.parametrize(
    "value",
    [{"nested": 1}, [1, 2], 1.5, ("a",)],
    ids=["nested-object", "list", "float", "tuple"],
)
def test_details_values_must_be_flat_integers_booleans_null_or_strings(value):
    with pytest.raises(ValidationError):
        RuleResult.model_validate(valid_entry(details={"k": value}))
    for ok in (1, True, None, "text"):
        assert RuleResult.model_validate(valid_entry(details={"k": ok})).details == {"k": ok}


def test_rule_result_key_is_reason_code_and_a_reason_key_is_rejected():
    assert "reason_code" in RuleResult.model_fields and "reason" not in RuleResult.model_fields
    swapped = {k: v for k, v in R4_ENTRY.items() if k != "reason_code"}
    swapped["reason"] = "active_return_exists"
    with pytest.raises(ValidationError):  # the free-text name cannot be used
        RuleResult.model_validate(swapped)
    with pytest.raises(ValidationError):  # nor added next to reason_code
        RuleResult.model_validate(valid_entry(reason="does not fit"))


@pytest.mark.parametrize(
    "reason_code",
    ["Does not fit", "", "within window", "A", "1abc", "x" * 65, "within-window"],
)
def test_reason_code_must_be_a_snake_case_code_of_at_most_64_characters(reason_code):
    with pytest.raises(ValidationError):
        RuleResult.model_validate(valid_entry(reason_code=reason_code))
    assert RuleResult.model_validate(valid_entry(reason_code="x" * 64)).reason_code == "x" * 64


# ---------- room for an R4 entry ----------


def test_an_r4_entry_fits_and_round_trips():
    stored = [R4_ENTRY]  # an R4 loser stores a one-entry array (R4 fires before R1 to R3 run)
    entries = [RuleResult.model_validate(entry) for entry in stored]
    assert [entry.model_dump(mode="json") for entry in entries] == stored
    assert RuleResult.model_validate_json(json.dumps(R4_ENTRY)).model_dump(mode="json") == R4_ENTRY
    assert entries[0].effect == "hard_deny" and entries[0].passed is False


# ---------- strictness ----------


@pytest.mark.parametrize(
    "bad",
    [
        {"rule_id": "r1"},
        {"rule_id": "R"},
        {"rule_id": "R1a"},
        {"rule_id": ""},
        {"effect": "deny"},
        {"effect": "limit"},
        {"passed": "yes"},
        {"passed": 1},
        {"extra_key": 1},
    ],
    ids=lambda bad: next(iter(bad)) + "=" + str(next(iter(bad.values()))),
)
def test_unknown_keys_and_bad_values_are_rejected(bad):
    with pytest.raises(ValidationError):
        RuleResult.model_validate(valid_entry(**bad))


@pytest.mark.parametrize("missing", sorted(RULE_KEYS))
def test_every_key_of_a_rule_result_is_required(missing):
    entry = {k: v for k, v in R4_ENTRY.items() if k != missing}
    with pytest.raises(ValidationError):
        RuleResult.model_validate(entry)


def test_unknown_keys_and_bad_values_are_rejected_in_a_policy_result(policy):
    good = all_pass(policy).model_dump(mode="json")
    for bad in (
        {**good, "extra": 1},
        {**good, "allowed_outcomes": ["approve", "maybe"]},
        {**good, "hard_deny": "no"},
        {key: value for key, value in good.items() if key != "policy_version"},
    ):
        with pytest.raises(ValidationError):
            PolicyResult.model_validate(bad)


# ---------- a result cannot contradict its own rule results ----------
# hard_deny is true exactly when a failed entry has effect hard_deny. allowed_outcomes is the
# derived tuple: ("deny",) after a hard deny, else ("deny", "escalate") when a failed entry has
# effect limit_outcomes, else ("approve", "deny", "escalate").


def r3_fail(policy):
    return run(policy, amount_minor=20001)


def changed(policy, build, **changes) -> dict:
    return {**build(policy).model_dump(mode="json"), **changes}


@pytest.mark.parametrize(
    ("build", "changes"),
    [
        pytest.param(all_pass, {"hard_deny": True}, id="all-pass-with-hard-deny-true"),
        pytest.param(r1_r2_fail, {"hard_deny": False}, id="hard-deny-failed-with-hard-deny-false"),
        pytest.param(r3_fail, {"hard_deny": True}, id="only-r3-failed-with-hard-deny-true"),
    ],
)
def test_a_result_whose_hard_deny_contradicts_its_rule_results_is_rejected(policy, build, changes):
    with pytest.raises(ValidationError):
        PolicyResult.model_validate(changed(policy, build, **changes))


@pytest.mark.parametrize(
    ("build", "allowed"),
    [
        pytest.param(r3_fail, ["approve", "deny", "escalate"], id="failed-r3-with-all-three"),
        pytest.param(all_pass, ["deny", "escalate"], id="all-pass-with-deny-and-escalate"),
        pytest.param(all_pass, ["deny"], id="all-pass-with-deny-only"),
        pytest.param(r1_r2_fail, ["deny", "escalate"], id="hard-deny-with-deny-and-escalate"),
        pytest.param(r1_r2_fail, ["approve", "deny", "escalate"], id="hard-deny-with-all-three"),
        pytest.param(all_pass, ["escalate", "deny", "approve"], id="all-pass-in-another-order"),
        pytest.param(r3_fail, ["escalate", "deny"], id="failed-r3-in-another-order"),
        pytest.param(all_pass, [], id="all-pass-with-none"),
    ],
)
def test_a_result_whose_allowed_outcomes_contradict_its_rule_results_is_rejected(
    policy, build, allowed
):
    with pytest.raises(ValidationError):
        PolicyResult.model_validate(changed(policy, build, allowed_outcomes=allowed))


def r4_only(**changes) -> dict:
    return {
        "policy_version": V1_POLICY_VERSION,
        "hard_deny": True,
        "allowed_outcomes": ["deny"],
        "rule_results": [R4_ENTRY],
        **changes,
    }


def test_the_r4_only_array_is_accepted_with_hard_deny_true_and_deny_only():
    result = PolicyResult.model_validate(r4_only())
    assert result.hard_deny is True and result.allowed_outcomes == ("deny",)
    assert result.rule_results_json() == [R4_ENTRY]


@pytest.mark.parametrize(
    "changes",
    [{"hard_deny": False}, {"allowed_outcomes": ["approve", "deny", "escalate"]}],
    ids=["hard-deny-false", "all-three-outcomes"],
)
def test_the_r4_only_array_with_a_contradicting_flag_is_rejected(changes):
    with pytest.raises(ValidationError):
        PolicyResult.model_validate(r4_only(**changes))


@pytest.mark.parametrize(
    "case",
    [
        {},
        {"amount_minor": 20001},
        {"category": "digital"},
        {"category": "digital", "amount_minor": 20001},
        {"currency": "EUR"},
        {"delivered_on": delivered(-3)},
    ],
    ids=["all-pass", "r3-only", "hard-deny-only", "hard-deny-and-r3", "currency", "future"],
)
def test_the_real_outputs_of_evaluate_still_validate(policy, case):
    result = run(policy, **case)
    assert PolicyResult.model_validate(result.model_dump(mode="json")) == result
    assert PolicyResult.model_validate_json(result.model_dump_json()) == result


# ---------- the shape of rule_results: R1, R2, R3 in order, or R4 alone as a failure ----------
# The flags below are always the ones that follow from the entries, so each rejection is caused by
# the SHAPE of rule_results and by nothing else.

R1, R2, R3 = EXAMPLE_ALL_PASS["rule_results"]  # all three pass in example 1
R4 = R4_ENTRY  # a failed hard-deny entry


def shaped(entries, hard_deny=False, allowed=("approve", "deny", "escalate")) -> dict:
    return {
        "policy_version": V1_POLICY_VERSION,
        "hard_deny": hard_deny,
        "allowed_outcomes": list(allowed),
        "rule_results": entries,
    }


def shaped_with_r4(entries) -> dict:
    return shaped(entries, hard_deny=True, allowed=("deny",))


BAD_SHAPES = {
    "empty": shaped([]),
    "only-r1": shaped([R1]),
    "only-r2": shaped([R2]),
    "only-r3": shaped([R3]),
    "r1-r3": shaped([R1, R3]),
    "r2-r3": shaped([R2, R3]),
    "r1-r2": shaped([R1, R2]),
    "r3-r2-r1": shaped([R3, R2, R1]),
    "r2-r1-r3": shaped([R2, R1, R3]),
    "r1-r2-r3-r4": shaped_with_r4([R1, R2, R3, R4]),
    "r4-r1-r2-r3": shaped_with_r4([R4, R1, R2, R3]),
    "r1-r4": shaped_with_r4([R1, R4]),
    "duplicated-r1": shaped([R1, R1, R2, R3]),
    "r1-r2-r3-r3": shaped([R1, R2, R3, R3]),
    "duplicated-r4": shaped_with_r4([R4, R4]),
    "r4-passed": shaped([{**R4, "passed": True}]),
}


@pytest.mark.parametrize("bad", BAD_SHAPES.values(), ids=BAD_SHAPES.keys())
def test_rule_results_of_the_wrong_shape_are_rejected(bad):
    with pytest.raises(ValidationError):
        PolicyResult.model_validate(bad)


def test_the_r1_r2_r3_array_is_accepted():
    result = PolicyResult.model_validate(shaped([R1, R2, R3]))
    assert [entry.rule_id for entry in result.rule_results] == ["R1", "R2", "R3"]
    assert result.rule_results_json() == [R1, R2, R3]


def test_the_r4_only_array_is_accepted_as_a_failure():
    result = PolicyResult.model_validate(shaped_with_r4([R4]))
    assert [(e.rule_id, e.passed) for e in result.rule_results] == [("R4", False)]
    assert result.hard_deny is True and result.allowed_outcomes == ("deny",)


def test_a_wrong_shape_is_rejected_even_when_built_from_python_objects(policy):
    # Not only from a dict: the real RuleResult objects of an evaluation, cut short or reordered.
    real = run(policy).rule_results
    for entries in (real[:2], real[1:], real[::-1], (), real + real[:1]):
        with pytest.raises(ValidationError):
            PolicyResult(
                policy_version=V1_POLICY_VERSION,
                hard_deny=False,
                allowed_outcomes=("approve", "deny", "escalate"),
                rule_results=entries,
            )


# ---------- the effect of a rule is fixed: R1, R2 and R4 hard_deny, R3 limit_outcomes ----------
# Each result below has the flags that FOLLOW from its entries as written (derive reads the
# entry's own effect), so the wrong effect is the only thing wrong with it.

R1_FAILED = EXAMPLE_R1_R2_FAIL["rule_results"][0]  # a failed R1, effect hard_deny
R2_FAILED = EXAMPLE_R1_R2_FAIL["rule_results"][1]  # a failed R2, effect hard_deny
R3_FAILED = {
    **R3,
    "passed": False,
    "reason_code": "above_ceiling",
    "details": {**R3["details"], "amount_minor": 20001},
}  # a failed R3, effect limit_outcomes


def effect_of(entry: dict, effect: str) -> dict:
    return {**entry, "effect": effect}


WRONG_EFFECTS = {
    "r1-failed-with-limit-outcomes": shaped(
        [effect_of(R1_FAILED, "limit_outcomes"), R2, R3],
        hard_deny=False,
        allowed=("deny", "escalate"),
    ),
    "r2-failed-with-limit-outcomes": shaped(
        [R1, effect_of(R2_FAILED, "limit_outcomes"), R3],
        hard_deny=False,
        allowed=("deny", "escalate"),
    ),
    "r3-failed-with-hard-deny": shaped(
        [R1, R2, effect_of(R3_FAILED, "hard_deny")], hard_deny=True, allowed=("deny",)
    ),
    "r3-passed-with-hard-deny": shaped([R1, R2, effect_of(R3, "hard_deny")]),
    "r1-passed-with-limit-outcomes": shaped([effect_of(R1, "limit_outcomes"), R2, R3]),
    "r4-failed-with-limit-outcomes": shaped(
        [effect_of(R4, "limit_outcomes")], hard_deny=False, allowed=("deny", "escalate")
    ),
}


@pytest.mark.parametrize("bad", WRONG_EFFECTS.values(), ids=WRONG_EFFECTS.keys())
def test_an_entry_whose_effect_is_not_the_fixed_one_for_its_rule_is_rejected(bad):
    with pytest.raises(ValidationError):
        PolicyResult.model_validate(bad)


def test_the_two_golden_examples_and_the_r3_failure_validate_with_the_fixed_effects():
    for example in (EXAMPLE_ALL_PASS, EXAMPLE_R1_R2_FAIL):
        assert PolicyResult.model_validate(example).model_dump(mode="json") == example
    r3_only = shaped([R1, R2, R3_FAILED], hard_deny=False, allowed=("deny", "escalate"))
    assert PolicyResult.model_validate(r3_only).allowed_outcomes == ("deny", "escalate")
