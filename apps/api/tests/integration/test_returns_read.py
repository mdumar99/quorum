"""The data-access read, through the real lifespan, as quorum_app (issue #50).

The pool lives on the lifespan's own event loop, so these tests run coroutines on it with
`client.portal.call`. The pool's connection is another session, so it cannot see a row the owner
inserted in a transaction that is rolled back: the row is COMMITTED by the owner and deleted by the
owner afterwards (the owner has the DELETE that quorum_app lacks). Keys are unique and prefixed
`p150-`, and a start-up sweep deletes the rows whose order_id starts with `p150-` (by order_id
prefix only) that an interrupted run left behind.
"""

import time
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from schema_helpers import insert_return, unique

from app.config import get_settings
from app.db.returns import get_return
from app.main import app

pytestmark = pytest.mark.integration

PREFIX = "p150-"


@pytest.fixture
def committed_return(owner_conn):
    """A return inserted and COMMITTED by the owner, removed by the owner afterwards."""
    owner_conn.execute("DELETE FROM public.returns WHERE order_id LIKE %s", (f"{PREFIX}%",))
    owner_conn.commit()
    values = {
        "order_id": unique(f"{PREFIX}order"),
        "order_line_id": unique(f"{PREFIX}line"),
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "EUR",
        "delivered_on": date(2026, 11, 1),
        "reason": "does not fit",
        "idempotency_key": unique(f"{PREFIX}key"),
    }
    return_id = insert_return(owner_conn, **values)
    owner_conn.commit()
    yield return_id, values
    owner_conn.rollback()
    owner_conn.execute("DELETE FROM public.returns WHERE id = %s", (return_id,))
    owner_conn.commit()


@pytest.fixture
def small_pool_client(monkeypatch):
    """The real lifespan with one pool connection and a short checkout timeout."""
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "1")
    monkeypatch.setenv("DB_POOL_CHECKOUT_TIMEOUT_SECONDS", "0.3")
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def test_a_committed_return_is_read_as_quorum_app(client, committed_return):
    return_id, values = committed_return
    pool = client.app.state.pool
    assert pool is not None

    async def read():
        async with pool.connection() as conn:
            return await get_return(conn, return_id)

    row = client.portal.call(read)
    assert row is not None
    assert type(row).__name__ == "ReturnRow"
    assert row.id == return_id
    for name, expected in values.items():
        assert getattr(row, name) == expected, name
    assert row.status == "processing"
    assert row.outcome is None
    assert row.attempt == 1
    assert row.as_of.tzinfo is not None


def test_an_unknown_id_is_read_as_none(client):
    pool = client.app.state.pool
    assert pool is not None

    async def read():
        async with pool.connection() as conn:
            return await get_return(conn, uuid4())

    assert client.portal.call(read) is None


def test_the_pool_connects_as_quorum_app(client):
    pool = client.app.state.pool
    assert pool is not None

    async def whoami():
        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT current_user")
            return (await cursor.fetchone())[0]

    assert client.portal.call(whoami) == "quorum_app"


def test_with_the_pool_exhausted_a_checkout_fails_after_the_checkout_timeout(small_pool_client):
    pool = small_pool_client.app.state.pool
    assert pool is not None

    async def scenario():
        async with pool.connection(timeout=5.0):  # warm-up: the one connection exists
            pass
        async with pool.connection():  # takes the only connection
            started = time.perf_counter()
            try:
                async with pool.connection():
                    pass
            except Exception as exc:
                return type(exc), time.perf_counter() - started
        return None, time.perf_counter() - started

    exc_type, elapsed = small_pool_client.portal.call(scenario)
    assert exc_type is not None, "the second checkout did not fail"
    assert (exc_type.__module__, exc_type.__qualname__) == ("psycopg_pool", "PoolTimeout")
    assert 0.25 <= elapsed < 3.0
