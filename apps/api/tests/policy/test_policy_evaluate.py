"""evaluate is pure, and its inputs are strict (#46: AC3 and AC10 at the case boundary).

Sources: ADR-0003 Decision 2 ("`as_of` is the database time ... never client-supplied") and the
choices approved on #46. No database is needed.
"""

import inspect
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from policy_examples import AS_OF, delivered, make_case, rule, run
from pydantic import ValidationError

from app.policy import PolicyCase, evaluate

# ---------- AC3: purity ----------


def test_evaluate_requires_as_of_and_has_no_defaults(policy):
    parameters = inspect.signature(evaluate).parameters
    assert list(parameters) == ["policy", "case", "as_of"]
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())
    with pytest.raises(TypeError):
        evaluate(policy, make_case())  # as_of is missing


def test_naive_as_of_is_rejected(policy):
    with pytest.raises(ValueError):
        evaluate(policy, make_case(), datetime(2026, 11, 14, 9, 30))


@pytest.mark.parametrize(
    "as_of", [date(2026, 11, 14), "2026-11-14T09:30:00+00:00", 1763112600, None]
)
def test_non_datetime_as_of_is_rejected(policy, as_of):
    with pytest.raises(TypeError):
        evaluate(policy, make_case(), as_of)


def test_inputs_are_not_mutated(policy):
    case = make_case(category="digital", delivered_on=date(2026, 9, 1))
    case_before, policy_before = case.model_dump(), policy.model_dump()
    evaluate(policy, case, AS_OF)
    assert case.model_dump() == case_before
    assert policy.model_dump() == policy_before
    with pytest.raises(ValidationError):  # the case and the policy are frozen
        case.amount_minor = 1
    with pytest.raises(ValidationError):
        policy.version = "v2"


def test_same_inputs_give_the_same_result(policy):
    case = make_case(category="digital", amount_minor=50000)
    first, second = evaluate(policy, case, AS_OF), evaluate(policy, case, AS_OF)
    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


@pytest.mark.parametrize("year", [2001, 2099])
def test_evaluate_does_not_read_the_clock(policy, year):
    # The result depends on as_of alone: far in the past or the future, 13 days is 13 days.
    as_of = datetime(year, 3, 1, 12, 0, tzinfo=UTC)
    result = run(policy, as_of=as_of, delivered_on=delivered(13, as_of))
    r1 = rule(result, "R1")
    assert r1.passed and r1.details["days_since_delivery"] == 13
    assert r1.details["as_of_date_utc"] == f"{year}-03-01"


def test_reason_is_not_part_of_the_case():
    # The return's free text never reaches the rules: the case has no such field and rejects it.
    with pytest.raises(ValidationError):
        make_case(reason="does not fit")
    assert set(PolicyCase.model_fields) == {"category", "amount_minor", "currency", "delivered_on"}


@pytest.mark.parametrize("extra", ["order_id", "order_line_id", "idempotency_key", "status"])
def test_the_case_rejects_every_field_the_rules_do_not_use(extra):
    with pytest.raises(ValidationError):
        make_case(**{extra: "x"})


def test_a_non_utc_aware_as_of_is_accepted(policy):
    as_of = datetime(2026, 11, 14, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    assert rule(run(policy, as_of=as_of), "R1").passed


# ---------- AC10: no float amounts at the case boundary ----------


@pytest.mark.parametrize(
    "amount",
    [1999.0, 19.99, True, False, "1999", None, Decimal("1999")],
    ids=["float", "float-with-cents", "true", "false", "string", "none", "decimal"],
)
def test_float_bool_string_and_decimal_amounts_are_rejected_at_the_case_boundary(amount):
    with pytest.raises(ValidationError):
        make_case(amount_minor=amount)


def test_negative_amount_is_rejected_and_zero_is_accepted():
    assert make_case(amount_minor=0).amount_minor == 0
    with pytest.raises(ValidationError):
        make_case(amount_minor=-1)


@pytest.mark.parametrize(
    "delivered_on",
    [datetime(2026, 11, 1), datetime(2026, 11, 1, tzinfo=UTC), "2026-11-01", None, 20261101],
    ids=["naive-datetime", "aware-datetime", "string", "none", "number"],
)
def test_delivery_date_must_be_a_date(delivered_on):
    with pytest.raises(ValidationError):
        make_case(delivered_on=delivered_on)


@pytest.mark.parametrize("field", ["category", "currency"])
def test_category_and_currency_must_not_be_empty(field):
    with pytest.raises(ValidationError):
        make_case(**{field: ""})


def test_case_currency_and_category_have_no_pattern_beyond_non_empty(policy):
    # R2 and R3 decide on whatever arrives; the case type does not raise for an odd value.
    result = run(policy, currency="usd", category="Not-A-Category!")
    assert rule(result, "R3").reason_code == "currency_mismatch"
    assert rule(result, "R3").passed is False
    assert result.allowed_outcomes == ("deny", "escalate")
    assert rule(result, "R2").passed is True
