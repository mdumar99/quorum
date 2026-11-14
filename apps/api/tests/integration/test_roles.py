"""The two database roles and their privileges (ADR-0002, Decision 3), as the real roles.

Needs the stack's Postgres with the bootstrap applied (`docker compose up -d --wait postgres
redis`, then `docker compose run --rm bootstrap`; CI runs the bootstrap before pytest).
DATABASE_URL must name quorum_app and OWNER_DATABASE_URL quorum_owner.
"""

import psycopg
import pytest
from psycopg import errors

from app.config import get_settings

pytestmark = pytest.mark.integration

OWNER = "quorum_owner"
APP = "quorum_app"


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


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def test_connections_are_the_expected_roles(owner_conn, app_conn):
    """Guards every other test here: a URL that pointed at the superuser would make them vacuous."""
    assert _one(owner_conn, "SELECT current_user") == OWNER
    assert _one(app_conn, "SELECT current_user") == APP


def test_neither_role_is_a_superuser(owner_conn):
    rows = owner_conn.execute(
        "SELECT rolname, rolsuper FROM pg_roles WHERE rolname IN (%s, %s) ORDER BY rolname",
        (APP, OWNER),
    ).fetchall()
    assert rows == [(APP, False), (OWNER, False)]


def test_owner_has_create_on_public_and_the_app_role_does_not(owner_conn):
    sql = "SELECT has_schema_privilege(%s, 'public', 'CREATE')"
    assert _one(owner_conn, sql, (OWNER,)) is True
    assert _one(owner_conn, sql, (APP,)) is False


def test_app_role_cannot_create_a_table_in_public(app_conn):
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        app_conn.execute("CREATE TABLE public.roles_probe_app (id integer)")


def test_default_privileges_give_the_app_role_select_and_insert_only(owner_conn):
    """A table the owner creates carries the default privileges of the bootstrap.

    The table is created in a transaction that is rolled back, so nothing is left behind. That
    also means another connection cannot see it, so the privileges are read from the table's own
    ACL with has_table_privilege rather than exercised with real statements as quorum_app.
    """
    owner_conn.execute("CREATE TABLE public.roles_probe (id integer)")
    granted = {
        privilege: _one(
            owner_conn,
            "SELECT has_table_privilege(%s, 'public.roles_probe', %s)",
            (APP, privilege),
        )
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
    }
    assert granted == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
        "REFERENCES": False,
        "TRIGGER": False,
    }
