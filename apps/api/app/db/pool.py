"""The database connection pool (ADR-0002, Decision 2; issue #50).

`psycopg_pool` is imported here and nowhere else. The pool is created closed (`open=False`: no event
loop and no connection yet) and started by the lifespan with `open(wait=False)`, so a database that
is down never blocks the boot (see docs/data-access.md for what was measured).
"""

import psycopg
from fastapi import Request
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool

from app.config import Settings

CONNECT_TIMEOUT_S = 2  # the same as /health's CHECK_TIMEOUT_S; a module constant, not a setting
POOL_CLOSE_TIMEOUT_S = 3.0  # below Docker's 10 s stop grace, so a stuck worker cannot hang a stop
POOL_STARTED_EVENT = "db_pool_started"
POOL_NAME = "quorum-api"  # fixed: a name derived from the URL could carry part of it into logs


class DatabaseUrlError(ValueError):
    """DATABASE_URL is not a valid connection string. The message never quotes the string."""


def create_pool(settings: Settings) -> AsyncConnectionPool:
    """Build the pool from the settings, closed. It makes no connection and needs no event loop.

    A string that is not a connection string stops the boot. psycopg's own error quotes the string,
    and with it the password, so that error is NOT passed on: the handler only remembers that the
    parse failed, and the new error is raised outside it. The new error therefore has no `__cause__`
    and no `__context__`, and its message holds no part of the string. This check is syntax only: a
    bad port or a bad `sslmode` parses fine and fails when the pool connects, which looks like a
    database that is down.
    """
    invalid = False
    try:
        conninfo_to_dict(settings.database_url)
    except psycopg.ProgrammingError:
        invalid = True
    if invalid:
        raise DatabaseUrlError("DATABASE_URL is not a valid connection string") from None
    return AsyncConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_checkout_timeout_seconds,
        kwargs={"connect_timeout": CONNECT_TIMEOUT_S},
        open=False,
        name=POOL_NAME,
    )


def get_pool(request: Request) -> AsyncConnectionPool:
    """The pool the lifespan started, for a FastAPI dependency."""
    return request.app.state.pool
