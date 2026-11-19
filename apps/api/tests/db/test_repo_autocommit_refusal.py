"""The write functions refuse an autocommit connection before touching it (issue #54).

In autocommit mode every statement would commit by itself, SAVEPOINT fails outside a transaction
block, and the decision row, the audit row and the compare-and-set could not be atomic. So
`insert_return`, `finalize_decided`, `finalize_failed` and `retry_failed` raise RuntimeError, and
raise it BEFORE running any SQL. The pool's connections are not autocommit (app/db/pool.py), so this
never fires for the real caller; these tests are what keeps the refusal from being removed unseen.

The stub connection reports `autocommit = True` and records, then fails on, ANY other attribute
access: "nothing was touched" is then a fact the test can check, not just the absence of a crash.
Only `finalize_failed` validates its own input; the other three receive models that were validated
when they were built. So the ordering case (bad input is a ValueError, raised before the autocommit
refusal) applies to `finalize_failed`.
"""

import pytest
from repo_unit_helpers import POLICY_VERSION, SAMPLE_ID, base_fields, policy_result, run, sample_row

from app.db.models import DecisionRecord
from app.db.returns import finalize_decided, finalize_failed, insert_return, retry_failed


class AutocommitConnection:
    """A connection with autocommit on that fails, and remembers, when anything else is used."""

    autocommit = True

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"the connection was touched (attribute {name!r})")


def _insert(conn):
    return insert_return(
        conn,
        base_fields(),
        idempotency_key="key-1",
        payload_hash="0" * 64,
        key_generation=1,
        policy_version=POLICY_VERSION,
    )


def _finalize_decided(conn):
    decision = DecisionRecord(
        outcome="deny", decided_by="rules", policy_result=policy_result("digital")
    )
    return finalize_decided(conn, return_id=SAMPLE_ID, attempt=1, decision=decision)


def _finalize_failed(conn):
    return finalize_failed(
        conn, return_id=SAMPLE_ID, attempt=1, error_type="llm_timeout", retry_after_seconds=None
    )


def _retry(conn):
    row = sample_row(status="failed", error_type="llm_timeout", retry_after_seconds=30, attempt=2)
    return retry_failed(conn, row)


CALLS = [
    pytest.param(_insert, id="insert_return"),
    pytest.param(_finalize_decided, id="finalize_decided"),
    pytest.param(_finalize_failed, id="finalize_failed"),
    pytest.param(_retry, id="retry_failed"),
]


def test_the_stub_connection_fails_and_remembers_when_anything_else_is_touched():
    conn = AutocommitConnection()
    assert conn.autocommit is True  # reading the flag is not a touch
    assert conn.touched == []
    with pytest.raises(AssertionError, match="was touched"):
        conn.execute  # noqa: B018
    assert conn.touched == ["execute"]


@pytest.mark.parametrize("make_call", CALLS)
def test_a_write_function_refuses_an_autocommit_connection_before_touching_it(make_call):
    conn = AutocommitConnection()
    with pytest.raises(RuntimeError, match="autocommit"):
        run(make_call(conn))
    assert conn.touched == []  # nothing but the flag was read: no SQL, no cursor, no savepoint


@pytest.mark.parametrize(
    ("error_type", "retry_after_seconds"),
    [
        pytest.param("not_an_error_type", None, id="unknown-error-type"),
        pytest.param("invalid_llm_output", 30, id="permanent-type-with-a-retry-after"),
        pytest.param("llm_timeout", 3601, id="retry-after-over-3600"),
    ],
)
def test_bad_input_to_finalize_failed_is_a_value_error_before_the_autocommit_refusal(
    error_type, retry_after_seconds
):
    conn = AutocommitConnection()
    with pytest.raises(ValueError):
        run(
            finalize_failed(
                conn,
                return_id=SAMPLE_ID,
                attempt=1,
                error_type=error_type,
                retry_after_seconds=retry_after_seconds,
            )
        )
    assert conn.touched == []
