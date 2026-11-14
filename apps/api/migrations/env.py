"""Alembic environment (ADR-0002, Decision 1).

* Runs as the owner role. The URL comes from OWNER_DATABASE_URL in the environment, never from
  the API's Settings: the owner URL must not become an API setting.
* Hand-written migrations: no autogenerate (target_metadata is None), because autogenerate does
  not see grants or default privileges, which carry the governance weight here.
* The version table lives in its own schema, `migrations`, created by the bootstrap
  (db/bootstrap.sql). The app role has no grant on it.
* Online only: a migration must run against the real database as the owner.
"""

import os
from logging.config import fileConfig

from alembic import context
from alembic.util import CommandError
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None:
    # Applies the [loggers] section of alembic.ini, so a run reports what it did.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The bootstrap creates this schema and gives only the owner access to it.
VERSION_TABLE_SCHEMA = "migrations"

# None: no autogenerate. Migrations are written by hand as plain SQL (op.execute).
target_metadata = None


def _sqlalchemy_url() -> str:
    """OWNER_DATABASE_URL, rewritten from the libpq form to SQLAlchemy's psycopg 3 driver."""
    url = os.environ.get("OWNER_DATABASE_URL")
    if not url:
        raise CommandError(
            "OWNER_DATABASE_URL is not set. Migrations run as the owner role: export it "
            "(it is in .env.example), for example: set -a; . ./.env; set +a"
        )
    for libpq_scheme in ("postgresql://", "postgres://"):
        if url.startswith(libpq_scheme):
            return "postgresql+psycopg://" + url[len(libpq_scheme) :]
    return url  # already names a SQLAlchemy driver


def run_migrations_online() -> None:
    engine = create_engine(_sqlalchemy_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=VERSION_TABLE_SCHEMA,
            transactional_ddl=True,  # Postgres DDL is transactional: a failed migration rolls back
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise CommandError(
        "Offline mode (--sql) is not supported: migrations run against the database as the owner."
    )
run_migrations_online()
