"""Connections as the real database roles, shared by the integration tests in this folder."""

import os

import psycopg
import pytest
from repo_fixtures import DatabaseRefused, check_test_database, delete_marked, new_run_marker, sweep

from app.config import get_settings


@pytest.fixture
def owner_conn(owner_database_url):
    conn = psycopg.connect(owner_database_url)
    try:
        yield conn
    finally:
        conn.rollback()  # every test that writes does so in a transaction it never commits
        conn.close()


@pytest.fixture
def app_conn():
    """The API's own connection: the URL comes from Settings, as in production."""
    conn = psycopg.connect(get_settings().database_url)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ---------- the tests that COMMIT data (issue #54) ----------
# Used explicitly by those tests, never autouse, so the rolled-back tests are untouched. All of it
# sits behind the guard of repo_fixtures: without QUORUM_TEST_DB=throwaway or CI=true nothing runs.


@pytest.fixture(scope="session")
def committed_data_is_allowed():
    """The guard, then the start-up sweep of stale `p154-` rows, once per session."""
    try:
        check_test_database(os.environ)
    except DatabaseRefused as refusal:
        pytest.fail(str(refusal))
    url = os.environ.get("OWNER_DATABASE_URL")
    if not url:
        pytest.fail("OWNER_DATABASE_URL is not set (the cleanup of committed test data needs it)")
    with psycopg.connect(url) as conn:
        sweep(conn)


@pytest.fixture
def marker(committed_data_is_allowed, owner_database_url):
    """A run marker for one test; the owner removes the rows that carry it afterwards."""
    value = new_run_marker()
    yield value
    with psycopg.connect(owner_database_url) as conn:
        delete_marked(conn, value)


@pytest.fixture
def app_url() -> str:
    """The API's own URL (quorum_app), from Settings as in production, never the owner's."""
    return get_settings().database_url
