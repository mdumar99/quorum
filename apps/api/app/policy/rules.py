"""The pure rules R1 to R3 and `evaluate` (ADR-0003, Decisions 2 and 3).

`evaluate(policy, case, as_of)` reads no clock, no database and no file: its result depends only
on its three arguments. All three rules ALWAYS run, in the order R1, R2, R3, and every result is
recorded, passed or failed, so a case that fails R1 and R2 shows both.

R4 (a duplicate return) is not here. It is a database invariant, written by the returns API.

The rule results hold only numbers, booleans, ISO dates and values that come from the POLICY file
(the matched excluded category, the policy's currency). They never echo the client's category or
currency, and the rules never see the return's free-text reason.
"""

from datetime import UTC, date, datetime

from app.policy.models import (
    Policy,
    PolicyCase,
    PolicyResult,
    R1Params,
    R2Params,
    R3Params,
    RuleResult,
    derive,
)


def rule_r1(params: R1Params, case: PolicyCase, as_of_date: date) -> RuleResult:
    """R1, return window: days from the delivery date to the UTC date of as_of, inclusive."""
    days = (as_of_date - case.delivered_on).days
    if days < 0:
        reason_code, passed = "delivered_after_as_of", False
    elif days > params.window_days:
        reason_code, passed = "outside_window", False
    else:
        reason_code, passed = "within_window", True
    return RuleResult(
        rule_id="R1",
        passed=passed,
        effect="hard_deny",
        reason_code=reason_code,
        details={
            "delivered_on": case.delivered_on.isoformat(),
            "as_of_date_utc": as_of_date.isoformat(),
            "days_since_delivery": days,
            "window_days": params.window_days,
        },
    )


def rule_r2(params: R2Params, case: PolicyCase) -> RuleResult:
    """R2, excluded categories. The category is matched exactly (case-sensitive)."""
    matched = next((c for c in params.excluded_categories if c == case.category), None)
    return RuleResult(
        rule_id="R2",
        passed=matched is None,
        effect="hard_deny",
        reason_code="category_allowed" if matched is None else "category_excluded",
        # `matched` is the value from the policy file, never the client's string.
        details={"category_excluded": matched is not None, "matched_category": matched},
    )


def rule_r3(params: R3Params, case: PolicyCase) -> RuleResult:
    """R3, amount ceiling, in the one currency of the policy.

    A different currency is not approvable and is not compared with the ceiling at all, so
    `currency_mismatch` is reported before `above_ceiling`.
    """
    currency_matches = case.currency == params.currency
    if not currency_matches:
        reason_code, passed = "currency_mismatch", False
    elif case.amount_minor > params.ceiling_minor:
        reason_code, passed = "above_ceiling", False
    else:
        reason_code, passed = "within_ceiling", True
    return RuleResult(
        rule_id="R3",
        passed=passed,
        effect="limit_outcomes",
        reason_code=reason_code,
        details={
            "amount_minor": case.amount_minor,
            "ceiling_minor": params.ceiling_minor,
            "currency": params.currency,  # the policy's currency, never the client's string
            "currency_matches": currency_matches,
        },
    )


def evaluate(policy: Policy, case: PolicyCase, as_of: datetime) -> PolicyResult:
    """Apply R1, R2 and R3 to a case. `as_of` must be a timezone-aware datetime (the database time).

    A non-datetime raises TypeError and a naive datetime raises ValueError: both are programming
    errors, not data errors. Days are counted against the date of `as_of` in UTC.
    """
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    as_of_date = as_of.astimezone(UTC).date()

    rule_results = (
        rule_r1(policy.rules.R1, case, as_of_date),
        rule_r2(policy.rules.R2, case),
        rule_r3(policy.rules.R3, case),
    )
    hard_deny, allowed_outcomes = derive(rule_results)
    return PolicyResult(
        policy_version=policy.policy_version,
        hard_deny=hard_deny,
        allowed_outcomes=allowed_outcomes,
        rule_results=rule_results,
    )
