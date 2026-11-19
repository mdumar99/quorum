"""`insert_audit` refuses an event type outside the closed list BEFORE any SQL (issue #54).

The connection passed in fails the moment anything touches it (`TouchedConnection`). So:
- an unknown type must raise ValueError and never reach the connection;
- a valid type must NOT be refused early: it reaches the connection, which is the AssertionError
  the stand-in raises. A stub that does nothing fails both ways (DID NOT RAISE).
The cases are literal lists, never the code's own EVENT_TYPES (an empty stub set would collect zero
cases and skip).
"""

from uuid import UUID

import pytest
from repo_unit_helpers import ALL_EVENT_TYPES, SAMPLE_ID, TouchedConnection, llm_record, run

from app.db.audit import insert_audit

UNKNOWN_TYPES = [
    "",
    "unknown",
    "decided",
    "RULES_DECIDED",
    "rules_decided ",
    " rules_decided",
    "failed_unknown",
    "failed_",
    "failed_LLM_TIMEOUT",
    "failed_llm_timeout_",
    "failed_failed_llm_timeout",
    "attempt_started",
]


@pytest.mark.parametrize("event_type", UNKNOWN_TYPES)
def test_a_type_outside_the_list_is_refused_before_any_sql(event_type):
    with pytest.raises(ValueError):
        run(insert_audit(TouchedConnection(), SAMPLE_ID, 1, event_type))


def test_failed_with_an_unknown_error_type_is_refused_before_any_sql():
    with pytest.raises(ValueError):
        run(insert_audit(TouchedConnection(), SAMPLE_ID, 1, "failed_bogus"))


def test_the_list_of_valid_types_is_the_eleven_of_the_issue():
    # Guards the parametrize below against an accidentally short list.
    assert len(ALL_EVENT_TYPES) == 11
    assert "superseded" in ALL_EVENT_TYPES
    assert "failed_abandoned" in ALL_EVENT_TYPES


@pytest.mark.parametrize("event_type", ALL_EVENT_TYPES)
def test_a_valid_type_is_not_refused_early_it_reaches_the_connection(event_type):
    with pytest.raises(AssertionError, match="was touched"):
        run(insert_audit(TouchedConnection(), SAMPLE_ID, 1, event_type))


def test_the_optional_arguments_do_not_change_the_early_check():
    other = UUID("11111111-2222-4333-8444-555555555555")
    with pytest.raises(ValueError):
        run(
            insert_audit(
                TouchedConnection(),
                SAMPLE_ID,
                2,
                "not_an_event",
                llm=llm_record(),
                conflicting_return_id=other,
            )
        )
    with pytest.raises(AssertionError, match="was touched"):
        run(
            insert_audit(
                TouchedConnection(),
                SAMPLE_ID,
                2,
                "r4_denied",
                llm=llm_record(),
                conflicting_return_id=other,
            )
        )
