"""The pool in the lifespan: started without waiting, closed with a timeout, never logged (#50).

Unit tests: no database. The URL points at a closed port (an IP literal: no DNS, refused at once),
so the pool's connection attempts fail instantly in the background. Logs are read from stdout with
capsys (structlog caches its logger, so `capture_logs` sees nothing). Types are checked by name:
`psycopg_pool` is not imported here.
"""

import json
import time
import traceback

import pytest
from db_helpers import BAD_URLS, MARKER, TIMING_MARGIN_S, URL_WITH_MARKER, WRONG_PORT_URL
from fastapi.testclient import TestClient

from app.db.pool import POOL_CLOSE_TIMEOUT_S, POOL_STARTED_EVENT, DatabaseUrlError
from app.main import app

POOL_TYPE = ("psycopg_pool.pool_async", "AsyncConnectionPool")
# Every malformed form in db_helpers.BAD_URLS; the ids are the form names, never the URLs.
every_bad_url = pytest.mark.parametrize("url", list(BAD_URLS.values()), ids=list(BAD_URLS))


def test_a_boot_with_no_database_does_not_wait_for_the_pool(boot_env):
    """open(wait=True) with a closed port would keep the boot waiting about the checkout timeout."""
    started = time.perf_counter()
    with TestClient(app):
        booted_in = time.perf_counter() - started
        pool = app.state.pool
        assert pool is not None
        assert (type(pool).__module__, type(pool).__qualname__) == POOL_TYPE
        assert pool.closed is False  # started
        kept = pool
    assert kept.closed is True  # closed by the shutdown
    assert booted_in < TIMING_MARGIN_S


def test_the_shutdown_closes_the_pool_with_its_timeout_and_does_not_hang(boot_env):
    calls = []
    with TestClient(app):
        pool = app.state.pool
        assert pool is not None
        original = pool.close

        async def recording_close(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        pool.close = recording_close
        left = time.perf_counter()
    shutdown_took = time.perf_counter() - left
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("timeout", args[0] if args else None) == POOL_CLOSE_TIMEOUT_S
    assert shutdown_took < TIMING_MARGIN_S


def test_the_boot_logs_db_pool_started_once_without_the_url(boot_env, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", URL_WITH_MARKER)
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    monkeypatch.setenv("DB_POOL_CHECKOUT_TIMEOUT_SECONDS", "1.5")
    with TestClient(app):
        pass
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    started = [e for e in events if e["event"] == POOL_STARTED_EVENT]
    assert len(started) == 1
    assert started[0]["min_size"] == 2
    assert started[0]["max_size"] == 3
    assert started[0]["checkout_timeout_seconds"] == 1.5
    leaked = MARKER in out or MARKER[:8] in out or "127.0.0.1" in out
    assert not leaked, "the log shows part of the database URL"


def test_a_boot_with_a_wrong_port_still_boots(boot_env, monkeypatch):
    """The URL check is syntax only (docs): a bad port looks like a database that is down."""
    monkeypatch.setenv("DATABASE_URL", WRONG_PORT_URL)
    with TestClient(app):
        assert app.state.pool is not None


@every_bad_url
def test_a_boot_with_a_malformed_url_fails_with_database_url_error(boot_env, monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(Exception) as excinfo:
        with TestClient(app):
            pass
    assert type(excinfo.value) is DatabaseUrlError


@every_bad_url
@pytest.mark.parametrize("which", ["str", "traceback"])
def test_the_marker_is_not_in_the_boot_failure(boot_env, monkeypatch, url, which):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(Exception) as excinfo:
        with TestClient(app):
            pass
    text = {
        "str": str(excinfo.value),
        "traceback": "".join(traceback.format_exception(excinfo.value)),
    }[which]
    leaked = MARKER in text or MARKER[:8] in text
    assert not leaked, f"part of the database URL is in the {which} of the boot failure"
