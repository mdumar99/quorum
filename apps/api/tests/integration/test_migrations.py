"""Migration set-up (ADR-0002, Decision 1): where Alembic's version table lives, and who sees it.

Needs the stack's Postgres with the bootstrap applied and the migrations at head:
    docker compose up -d --wait postgres redis
    docker compose run --rm bootstrap
    cd apps/api && uv run alembic upgrade head      # needs OWNER_DATABASE_URL
CI runs the same two steps before pytest. DATABASE_URL must name quorum_app and
OWNER_DATABASE_URL quorum_owner. owner_conn and app_conn are in conftest.py (this folder).
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors

pytestmark = pytest.mark.integration

OWNER = "quorum_owner"
APP = "quorum_app"

# apps/api/alembic.ini, found from this file so the test works from any directory.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_version_table_is_in_the_migrations_schema_and_is_at_head(owner_conn):
    schemas = owner_conn.execute(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = 'alembic_version'"
    ).fetchall()
    assert schemas == [("migrations",)]  # in its own schema, and not in public
    # The history is linear (one head), and the database is at that head, whichever revision it is.
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    assert len(script.get_heads()) == 1
    versions = owner_conn.execute("SELECT version_num FROM migrations.alembic_version").fetchall()
    assert versions == [(script.get_current_head(),)]


def test_app_role_has_no_usage_on_the_migrations_schema(owner_conn, app_conn):
    usage = owner_conn.execute("SELECT has_schema_privilege(%s, 'migrations', 'USAGE')", (APP,))
    assert usage.fetchone()[0] is False
    # And for real, as the app role: it cannot read the version table.
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
        app_conn.execute("SELECT version_num FROM migrations.alembic_version")


def test_owner_owns_the_migrations_schema(owner_conn):
    row = owner_conn.execute(
        "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'migrations'"
    ).fetchone()
    assert row == (OWNER,)
