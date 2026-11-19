"""`lookup_key` refuses a bad TTL BEFORE any SQL (issue #54, criterion 3).

`ttl_seconds` must be positive and finite: zero or less would make every key expired or no key
expire, and Postgres cannot build an interval from nan or infinity. The check raises ValueError
before the connection is touched (a stand-in fails if it is). A valid TTL reaches the connection.
Nan and infinity are refused too: this goes one step past "<= 0" and is flagged in the review.
"""

import pytest
from repo_unit_helpers import TouchedConnection, run

from app.db.returns import lookup_key


def call(ttl_seconds):
    return run(lookup_key(TouchedConnection(), "key-1", "0" * 64, ttl_seconds))


@pytest.mark.parametrize(
    "ttl", [0, 0.0, -1, -0.001, float("-inf"), float("inf"), float("nan")], ids=str
)
def test_a_ttl_that_is_not_positive_and_finite_is_refused_before_any_sql(ttl):
    with pytest.raises(ValueError):
        call(ttl)


@pytest.mark.parametrize("ttl", [0.001, 1, 86400, 86400.5])
def test_a_valid_ttl_is_not_refused_early(ttl):
    with pytest.raises(AssertionError, match="was touched"):
        call(ttl)
