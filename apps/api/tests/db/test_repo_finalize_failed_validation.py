"""`finalize_failed` refuses a bad `retry_after_seconds` or error type BEFORE any SQL (issue #54).

Unit tests raise ValueError before the SQL, using a connection that fails when touched. The
database stays a second wall (`returns_retry_after_ck`: 0 to 3600, and `returns_state_ck`: a
`failed` row with `invalid_llm_output` has no retry_after_seconds); the integration tests prove it.
Refused here:
- a retry_after_seconds with the permanent error type (the schema allows none);
- a retry_after_seconds outside 0 to 3600, or not a whole number (1.5, nan);
- an error type outside the closed list.
Valid inputs must NOT be refused early: they reach the connection (the AssertionError of the
stand-in). The cases are literal lists.
"""

import pytest
from repo_unit_helpers import SAMPLE_ID, TRANSIENT_FIVE, TouchedConnection, run

from app.db.returns import finalize_failed


def call(**overrides):
    values = {
        "return_id": SAMPLE_ID,
        "attempt": 1,
        "error_type": "llm_timeout",
        "retry_after_seconds": None,
    }
    values.update(overrides)
    return run(finalize_failed(TouchedConnection(), **values))


@pytest.mark.parametrize("seconds", [0, 1, 30, 3600])
def test_a_retry_after_with_the_permanent_error_type_is_refused(seconds):
    with pytest.raises(ValueError):
        call(error_type="invalid_llm_output", retry_after_seconds=seconds)


@pytest.mark.parametrize("seconds", [-1, 3601, 10**9, 1.5, float("nan")], ids=repr)
@pytest.mark.parametrize("error_type", TRANSIENT_FIVE)
def test_a_retry_after_outside_0_to_3600_or_not_a_whole_number_is_refused(error_type, seconds):
    # 1.5 and nan are inside no integer range: the column is an integer, and 12b rounds a float
    # from the provider up before it calls the repository, so the repository refuses a float.
    with pytest.raises(ValueError):
        call(error_type=error_type, retry_after_seconds=seconds)


@pytest.mark.parametrize(
    "error_type", ["bogus", "", "LLM_TIMEOUT", "failed_llm_timeout", "capacity "]
)
def test_an_error_type_outside_the_list_is_refused(error_type):
    with pytest.raises(ValueError):
        call(error_type=error_type)


@pytest.mark.parametrize("seconds", [None, 0, 30, 3600])
@pytest.mark.parametrize("error_type", TRANSIENT_FIVE)
def test_a_valid_transient_failure_is_not_refused_early(error_type, seconds):
    with pytest.raises(AssertionError, match="was touched"):
        call(error_type=error_type, retry_after_seconds=seconds)


def test_the_permanent_error_type_without_a_retry_after_is_not_refused_early():
    with pytest.raises(AssertionError, match="was touched"):
        call(error_type="invalid_llm_output", retry_after_seconds=None)
