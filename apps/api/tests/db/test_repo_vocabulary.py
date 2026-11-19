"""The closed lists of the returns write side (issue #54; ADR-0004, Decision 2).

Three independent sources must agree: the code (app/db/vocabulary.py), the schema (parsed from
db/schema.sql, never retyped) and the ADR (written out in the test). Nothing here is parametrized
over a set from the code under test: an empty stub set would collect zero cases and skip. Cases are
literal lists, or the code's set is compared whole.
"""

import pytest
from repo_schema import schema_error_types, schema_outcomes, schema_statuses
from repo_unit_helpers import (
    ADR_PERMANENT,
    ADR_TRANSIENT,
    EXPECTED_EVENT_TYPES,
    SIX_ERROR_TYPES,
)

from app.db.vocabulary import (
    ERROR_TYPES,
    EVENT_TYPES,
    OUTCOMES,
    PERMANENT_ERROR_TYPES,
    STATUSES,
    TRANSIENT_ERROR_TYPES,
    failed_event,
)
from app.llm.errors import ERROR_CLASSES

# ---------- against db/schema.sql ----------


def test_error_types_equal_the_schema_list():
    assert ERROR_TYPES == schema_error_types()


def test_statuses_equal_the_schema_list():
    assert STATUSES == schema_statuses()


def test_outcomes_equal_the_schema_list():
    assert OUTCOMES == schema_outcomes()


def test_the_schema_lists_are_the_ones_the_adr_names():
    # A guard on the parser itself: if it ever parsed the wrong constraint, the three tests above
    # would compare the code with the wrong list and could still pass.
    assert schema_error_types() == frozenset(SIX_ERROR_TYPES)
    assert schema_statuses() == {"processing", "decided", "pending_review", "failed"}
    assert schema_outcomes() == {"approve", "deny", "escalate"}


# ---------- against ADR-0004 ----------


def test_the_transient_set_is_the_adr_list():
    assert TRANSIENT_ERROR_TYPES == ADR_TRANSIENT


def test_the_permanent_set_is_the_adr_list():
    assert PERMANENT_ERROR_TYPES == ADR_PERMANENT


def test_transient_and_permanent_are_disjoint_and_together_are_every_error_type():
    assert TRANSIENT_ERROR_TYPES.isdisjoint(PERMANENT_ERROR_TYPES)
    assert TRANSIENT_ERROR_TYPES | PERMANENT_ERROR_TYPES == ERROR_TYPES
    assert len(ERROR_TYPES) == 6


# ---------- against the LLM error classes ----------


def test_the_llm_error_classes_are_the_four_provider_errors():
    # `capacity` and `abandoned` are not provider errors (app/llm/errors.py), so the classes cover
    # four of the six types. This also keeps the parametrized test below from being empty.
    assert set(ERROR_CLASSES) == {
        "llm_timeout",
        "rate_limited",
        "provider_unavailable",
        "invalid_llm_output",
    }


@pytest.mark.parametrize("error_type", sorted(ERROR_CLASSES))
def test_every_llm_error_class_agrees_with_its_types_membership(error_type):
    error_class = ERROR_CLASSES[error_type]
    if error_class.transient:
        assert error_type in TRANSIENT_ERROR_TYPES
        assert error_type not in PERMANENT_ERROR_TYPES
    else:
        assert error_type in PERMANENT_ERROR_TYPES
        assert error_type not in TRANSIENT_ERROR_TYPES


# ---------- audit event types ----------


def test_the_event_types_are_the_literal_list():
    # rules_decided, llm_decided, escalated, r4_denied, superseded, and failed_ plus each of the six
    # error types: eleven in all.
    assert EVENT_TYPES == EXPECTED_EVENT_TYPES
    assert len(EVENT_TYPES) == 11


@pytest.mark.parametrize("error_type", SIX_ERROR_TYPES)
def test_failed_event_names_each_error_type(error_type):
    assert failed_event(error_type) == f"failed_{error_type}"
    assert failed_event(error_type) in EXPECTED_EVENT_TYPES


@pytest.mark.parametrize(
    "unknown",
    [
        "",
        "unknown",
        "LLM_TIMEOUT",
        "llm_timeout ",
        " llm_timeout",
        "failed_llm_timeout",
        "capacity\n",
    ],
)
def test_failed_event_refuses_an_unknown_error_type(unknown):
    with pytest.raises(ValueError):
        failed_event(unknown)
