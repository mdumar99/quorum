"""A checkout that cannot get a connection fails with PoolTimeout after the checkout timeout (#50).

No database: the URL is a closed port, so no connection ever arrives. The exception is checked by
its module and name (`psycopg_pool` is not imported by the tests).
"""

import asyncio
import time

import pytest
from db_helpers import make_settings

from app.db.pool import create_pool

pytestmark = pytest.mark.usefixtures("no_pool_env")


def test_a_checkout_with_no_reachable_database_fails_with_pool_timeout_after_the_timeout():
    settings = make_settings(
        db_pool_min_size=1, db_pool_max_size=1, db_pool_checkout_timeout_seconds=0.3
    )

    async def scenario():
        pool = create_pool(settings)
        await pool.open(wait=False)
        started = time.perf_counter()
        try:
            async with pool.connection():
                pass
        except Exception as exc:
            return type(exc), time.perf_counter() - started
        finally:
            await pool.close(timeout=1.0)
        return None, time.perf_counter() - started

    exc_type, elapsed = asyncio.run(scenario())
    assert exc_type is not None, "the checkout did not fail"
    assert (exc_type.__module__, exc_type.__qualname__) == ("psycopg_pool", "PoolTimeout")
    assert 0.25 <= elapsed < 2.0
