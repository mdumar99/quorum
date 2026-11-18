"""create_pool builds the pool from the settings and does not start it (issue #50).

Plain (non-async) tests on purpose: `open=False` must need no event loop, so these also show that.
`psycopg_pool` is not imported here: types are checked by name.
"""

from types import SimpleNamespace

import pytest
from db_helpers import MARKER, URL_WITH_MARKER, WRONG_PORT_URL, make_settings

from app.db.pool import (
    CONNECT_TIMEOUT_S,
    POOL_CLOSE_TIMEOUT_S,
    POOL_NAME,
    POOL_STARTED_EVENT,
    create_pool,
    get_pool,
)

pytestmark = pytest.mark.usefixtures("no_pool_env")


def test_the_module_constants():
    assert CONNECT_TIMEOUT_S == 2
    assert POOL_CLOSE_TIMEOUT_S == 3.0
    assert POOL_STARTED_EVENT == "db_pool_started"


def test_the_pool_is_an_async_connection_pool():
    pool = create_pool(make_settings())
    assert (type(pool).__module__, type(pool).__qualname__) == (
        "psycopg_pool.pool_async",
        "AsyncConnectionPool",
    )


def test_the_pool_takes_its_sizes_and_the_checkout_timeout_from_the_settings():
    settings = make_settings(
        db_pool_min_size=2, db_pool_max_size=5, db_pool_checkout_timeout_seconds=1.5
    )
    pool = create_pool(settings)
    assert (pool.min_size, pool.max_size, pool.timeout) == (2, 5, 1.5)


def test_the_pool_connects_with_the_database_url_and_nothing_else():
    """The app role's URL. Settings has no owner URL (see tests/test_config.py), so no other one."""
    settings = make_settings()
    assert create_pool(settings).conninfo == settings.database_url


def test_the_connect_timeout_is_passed_to_every_connection():
    assert create_pool(make_settings()).kwargs == {"connect_timeout": CONNECT_TIMEOUT_S}


def test_create_pool_does_not_start_the_pool():
    assert create_pool(make_settings()).closed is True


def test_every_call_builds_a_new_pool():
    settings = make_settings()
    assert create_pool(settings) is not create_pool(settings)


def test_a_well_formed_url_with_a_wrong_port_still_builds_a_pool():
    """The URL check is syntax only: a bad port fails when it connects, not here (docs)."""
    pool = create_pool(make_settings(database_url=WRONG_PORT_URL))
    assert pool.conninfo == WRONG_PORT_URL


def test_get_pool_returns_the_pool_kept_on_app_state():
    sentinel = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=sentinel)))
    assert get_pool(request) is sentinel


def test_the_pool_has_the_fixed_name_and_the_name_holds_no_part_of_the_url():
    """The pool's name is written into its own log lines, so it must not come from the URL.

    A name derived from the database URL would carry part of it (the password included) into the
    log. The marker is made up and never printed: every check is a boolean, so a failing assertion
    cannot show it. The URL here carries the marker in the password position.
    """
    pool = create_pool(make_settings(database_url=URL_WITH_MARKER))
    name = str(pool.name)
    is_the_fixed_name = name == POOL_NAME  # a boolean first: a failing `==` would print the name
    assert is_the_fixed_name, "the pool name is not the fixed POOL_NAME"
    leaked = MARKER in name or MARKER[:8] in name or "127.0.0.1" in name or "@" in name
    assert not leaked, "the pool name holds part of the database URL"
