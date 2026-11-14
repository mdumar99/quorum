"""The types of the policy package (ADR-0003, Decision 2, and the choices approved on #46).

Three groups:
- the policy file: `PolicyFile` is what a versions/<name>.toml must contain, and `Policy` is that
  content plus its content hash. Both are strict, frozen and reject unknown keys.
- the case: `PolicyCase` is the only input the rules see. It has no free-text `reason`.
- the results: `RuleResult` and `PolicyResult`. `decisions.rule_results` stores the list of
  rule results (`PolicyResult.rule_results_json()`); the other parts of a PolicyResult have their
  own columns. Nothing free-form can enter a result: `reason_code` and the keys of `details` are
  short snake_case codes, and the string values of `details` are at most 64 characters.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

# ---------- errors ----------


class PolicyError(Exception):
    """A policy file or a pinned policy_version is not acceptable. Nothing is loaded."""


class PolicyNotFoundError(PolicyError):
    """A well-formed pinned policy_version names a version that has no file."""


class PolicyHashMismatchError(PolicyError):
    """The file of a pinned version no longer has the content hash the version was pinned with."""


# ---------- the policy file ----------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _list_to_tuple(value):
    # TOML gives lists. A strict tuple field would reject them, so they are converted first;
    # anything that is not a list (a string, a number) is left alone and fails the tuple check.
    return tuple(value) if isinstance(value, list) else value


# At most 64 characters: R2 records the matched category in its details, whose strings are capped.
CategoryName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
VersionName = Annotated[str, StringConstraints(pattern=r"^v[0-9]+$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class R1Params(_Strict):
    window_days: Annotated[int, Field(ge=0)]


class R2Params(_Strict):
    excluded_categories: Annotated[tuple[CategoryName, ...], BeforeValidator(_list_to_tuple)]

    @field_validator("excluded_categories")
    @classmethod
    def _unique_and_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("excluded_categories must not contain duplicates")
        return tuple(sorted(value))  # a set in effect: the order cannot change the content hash


class R3Params(_Strict):
    ceiling_minor: Annotated[int, Field(ge=0)]
    currency: CurrencyCode


class PolicyRules(_Strict):
    R1: R1Params
    R2: R2Params
    R3: R3Params


class PolicyFile(_Strict):
    """The content of a policy file."""

    version: VersionName
    rules: PolicyRules


class Policy(PolicyFile):
    """A validated policy: the file content plus its content hash (see loader.py).

    A Policy must always come from `load_policy` or `load_current_policy`. This type trusts the
    `content_hash` it is given: building one by hand with another hash is not checked here.
    """

    content_hash: ContentHash

    @property
    def policy_version(self) -> str:
        """The string pinned on a return and copied on its decision: `<name>:<64 hex>`."""
        return f"{self.version}:{self.content_hash}"


# ---------- the case ----------


class PolicyCase(_Strict):
    """What the rules see of a return. The free-text `reason` is deliberately not a field."""

    category: Annotated[str, Field(min_length=1)]
    amount_minor: Annotated[int, Field(ge=0)]  # integer minor units: float, bool and str fail
    currency: Annotated[str, Field(min_length=1)]
    delivered_on: date  # a calendar date: a datetime or a string fails


# ---------- the results ----------

Effect = Literal["hard_deny", "limit_outcomes"]
Outcome = Literal["approve", "deny", "escalate"]

# One shape for reason_code and for the keys of details: a short snake_case code.
Code = Annotated[StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
DetailValue = StrictInt | StrictBool | Annotated[StrictStr, StringConstraints(max_length=64)] | None


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleResult(_Result):
    """What one rule found. `effect` is what a FAILURE of this rule does (fixed per rule)."""

    rule_id: Annotated[StrictStr, StringConstraints(pattern=r"^R[0-9]+$")]
    passed: StrictBool
    effect: Effect
    reason_code: Code
    details: dict[Code, DetailValue]


# The effect of a rule is fixed: what a failure of it does. A result carrying another effect for
# a rule id is rejected, so the flags derived from the effects cannot be steered by the data.
EFFECT_OF_RULE: dict[str, Effect] = {
    "R1": "hard_deny",
    "R2": "hard_deny",
    "R3": "limit_outcomes",
    "R4": "hard_deny",
}


def derive(rule_results) -> tuple[bool, tuple[Outcome, ...]]:
    """`hard_deny` and `allowed_outcomes` that follow from the rule results (ADR-0003, Decision 3).

    A failed hard-deny rule gives ("deny",). Otherwise a failed outcome-limiting rule gives
    ("deny", "escalate"). Otherwise all three, in the fixed order approve, deny, escalate.
    """
    failed = [entry for entry in rule_results if not entry.passed]
    if any(entry.effect == "hard_deny" for entry in failed):
        return True, ("deny",)
    if any(entry.effect == "limit_outcomes" for entry in failed):
        return False, ("deny", "escalate")
    return False, ("approve", "deny", "escalate")


class PolicyResult(_Result):
    """The outcome of evaluate.

    `rule_results` has exactly one of two shapes: R1, R2, R3 in that order (what evaluate returns),
    or R4 alone as a failure (what the returns API stores for a duplicate return, because R4 fires
    at insert before R1 to R3 run). An empty, partial, reordered or repeated array is rejected,
    and so is an entry whose effect is not the fixed one for its rule. The result also cannot
    contradict its own rule results.
    """

    policy_version: Annotated[StrictStr, StringConstraints(pattern=r"^v[0-9]+:[0-9a-f]{64}$")]
    hard_deny: StrictBool
    allowed_outcomes: tuple[Outcome, ...]
    rule_results: tuple[RuleResult, ...]

    @model_validator(mode="after")
    def _agrees_with_the_rule_results(self):
        ids = tuple(entry.rule_id for entry in self.rule_results)
        if ids == ("R4",):
            if self.rule_results[0].passed:
                raise ValueError("an R4 entry must be a failure")
        elif ids != ("R1", "R2", "R3"):
            raise ValueError("rule_results must be R1, R2, R3 in that order, or R4 alone")
        for entry in self.rule_results:
            if entry.effect != EFFECT_OF_RULE[entry.rule_id]:
                raise ValueError(
                    f"{entry.rule_id} has the fixed effect {EFFECT_OF_RULE[entry.rule_id]}"
                )
        hard_deny, allowed_outcomes = derive(self.rule_results)
        if self.hard_deny != hard_deny:
            raise ValueError("hard_deny contradicts the rule results")
        if self.allowed_outcomes != allowed_outcomes:
            raise ValueError("allowed_outcomes contradicts the rule results")
        return self

    def rule_results_json(self) -> list[dict]:
        """The list stored in `decisions.rule_results`: plain JSON types only."""
        return [entry.model_dump(mode="json") for entry in self.rule_results]
