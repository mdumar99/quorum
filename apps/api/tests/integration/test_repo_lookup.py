"""Criterion 3: `lookup_key` (issue #54; ADR-0004, Decision 4).

The latest generation of a key is classified: none; expired (the latest row's `created_at` is
older than the TTL on the database clock); a different payload hash; otherwise by status: in flight
(`processing`), retryable (`failed` with a transient error) or replay (everything else). Expiry is
tested before the payload hash, because an expired key is a new submission.

The owner backdates `created_at` by `TTL + 2 s` (expired) or `TTL - 2 s` (current), which pins the
direction of the comparison with a margin on both sides. The BOUNDARY itself (`>` versus `>=`, a row
exactly TTL old) cannot be observed: the TTL must be positive and real time keeps moving, so no
test can build a row whose age is exactly the TTL. This module does not claim to cover it. The
same-transaction test below pins the CLOCK SOURCE (the age is measured with the database's
transaction time), not the boundary. No clock is mocked.
"""

import pytest
from repo_data import (
    HASH_A,
    HASH_B,
    PV,
    TRANSIENT_FIVE,
    TTL,
    backdate_created,
    backdate_updated,
    key_for,
    new_return,
    owner_row,
    run,
    with_conn,
)

from app.db.models import ExpiredKey, InFlight, NoKey, PayloadMismatch, Replay, Retryable
from app.db.returns import lookup_key

pytestmark = pytest.mark.integration


async def lookup(url, key, payload_hash=HASH_A, ttl=TTL):
    return await with_conn(url, lambda c: lookup_key(c, key, payload_hash, ttl), commit=False)


# (id, the columns of the row, the expected result class)
STATES = [
    pytest.param({"status": "processing"}, InFlight, id="processing"),
    pytest.param({"status": "decided", "outcome": "approve"}, Replay, id="decided-approve"),
    pytest.param({"status": "decided", "outcome": "deny"}, Replay, id="decided-deny"),
    pytest.param({"status": "pending_review", "outcome": "escalate"}, Replay, id="pending_review"),
    pytest.param(
        {"status": "failed", "error_type": "invalid_llm_output"}, Replay, id="failed-permanent"
    ),
    *[
        pytest.param(
            {"status": "failed", "error_type": t, "retry_after_seconds": 30},
            Retryable,
            id=f"failed-{t}",
        )
        for t in TRANSIENT_FIVE
    ],
]


def test_an_unknown_key_is_none_and_the_first_generation_is_next(marker, app_url):
    assert run(lookup(app_url, key_for(marker))) == NoKey(next_generation=1)


@pytest.mark.parametrize(("columns", "expected"), STATES)
def test_each_current_state_is_classified(marker, owner_conn, app_url, columns, expected):
    return_id = new_return(owner_conn, marker, **columns)
    result = run(lookup(app_url, key_for(marker)))
    assert isinstance(result, expected)
    assert result.row.id == return_id
    assert result.row.model_dump() == owner_row(owner_conn, return_id)


@pytest.mark.parametrize(
    "columns",
    [{"status": "processing"}, {"status": "decided", "outcome": "deny"}],
    ids=["processing", "decided"],
)
def test_a_different_payload_hash_is_a_mismatch(marker, owner_conn, app_url, columns):
    return_id = new_return(owner_conn, marker, payload_hash=HASH_A, **columns)
    result = run(lookup(app_url, key_for(marker), payload_hash=HASH_B))
    assert isinstance(result, PayloadMismatch)
    assert result.row.id == return_id


@pytest.mark.parametrize(
    "columns",
    [
        {"status": "processing"},
        {"status": "decided", "outcome": "approve"},
        {"status": "failed", "error_type": "llm_timeout"},
    ],
    ids=["processing", "decided", "failed"],
)
def test_a_key_older_than_the_ttl_is_expired_and_the_next_generation_is_2(
    marker, owner_conn, app_url, columns
):
    return_id = new_return(owner_conn, marker, **columns)
    backdate_created(owner_conn, return_id, TTL + 2)
    result = run(lookup(app_url, key_for(marker)))
    assert isinstance(result, ExpiredKey)
    assert result.next_generation == 2
    assert result.row.id == return_id


def test_a_key_younger_than_the_ttl_is_current(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing")
    backdate_created(owner_conn, return_id, TTL - 2)
    result = run(lookup(app_url, key_for(marker)))
    assert isinstance(result, InFlight)
    assert result.row.id == return_id


def test_in_the_same_transaction_nothing_is_older_than_a_tiny_ttl(marker, app_url):
    async def action(conn):
        await conn.execute(
            "INSERT INTO public.returns (order_id, order_line_id, category, amount_minor, "
            "currency, delivered_on, reason, status, policy_version, idempotency_key, "
            "payload_hash) VALUES (%s, 'line-1', 'shoes', 1999, 'USD', '2026-11-01', "
            "'does not fit', 'processing', %s, %s, %s)",
            (f"{marker}order", PV, key_for(marker), HASH_A),
        )
        # This pins the CLOCK SOURCE, not the boundary. created_at and now() are the same
        # instant here (the transaction's start), so the database-measured age is 0 and the row
        # is not older than the tiny TTL. An age computed with the application's clock would be
        # unrelated to created_at and would say otherwise. ">" versus ">=" is not observable:
        # the TTL must be positive, and real time moves.
        return await lookup_key(conn, key_for(marker), HASH_A, 0.001)

    assert isinstance(run(with_conn(app_url, action, commit=False)), InFlight)


def test_expiry_wins_over_a_payload_mismatch(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, payload_hash=HASH_A)
    backdate_created(owner_conn, return_id, TTL + 2)
    result = run(lookup(app_url, key_for(marker), payload_hash=HASH_B))
    assert isinstance(result, ExpiredKey)


def test_the_age_is_measured_from_created_at_not_updated_at(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing")
    backdate_created(owner_conn, return_id, TTL + 2)
    backdate_updated(owner_conn, return_id, 0)  # touched just now
    assert isinstance(run(lookup(app_url, key_for(marker))), ExpiredKey)


def two_generations(owner_conn, marker):
    """Generation 1 was denied (inactive); generation 2 is processing, on the same order line."""
    first = new_return(owner_conn, marker, status="decided", outcome="deny", key_generation=1)
    second = new_return(owner_conn, marker, status="processing", key_generation=2)
    return first, second


def test_the_latest_of_two_generations_is_the_one_used(marker, owner_conn, app_url):
    _, second = two_generations(owner_conn, marker)
    result = run(lookup(app_url, key_for(marker)))
    assert isinstance(result, InFlight)
    assert result.row.id == second
    assert result.row.key_generation == 2


def test_an_expired_latest_generation_gives_the_next_one_after_it(marker, owner_conn, app_url):
    _, second = two_generations(owner_conn, marker)
    backdate_created(owner_conn, second, TTL + 2)
    result = run(lookup(app_url, key_for(marker)))
    assert isinstance(result, ExpiredKey)
    assert result.row.id == second
    assert result.next_generation == 3


def test_an_old_first_generation_does_not_expire_a_fresh_second_one(marker, owner_conn, app_url):
    first, second = two_generations(owner_conn, marker)
    backdate_created(owner_conn, first, TTL * 10)
    result = run(lookup(app_url, key_for(marker)))
    assert isinstance(result, InFlight)
    assert result.row.id == second


def test_a_lookup_writes_nothing(marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker, status="processing")
    before = owner_row(owner_conn, return_id)
    run(lookup(app_url, key_for(marker)))
    assert owner_row(owner_conn, return_id) == before
