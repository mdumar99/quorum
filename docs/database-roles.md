# Database roles, migrations and the drift check

This page is for a developer who has just cloned the repo. It covers the two database roles, where their passwords live, how to add a migration, what the drift check protects, and how to change a password. The decisions behind it are in [ADR-0002](adr/0002-migrations-data-access-roles.md).

## The two roles
- `quorum_owner` runs the migrations and owns every object the migrations create (tables, the `migrations` schema). `quorum_app` is the only role the API uses. Neither is a superuser. The superuser (`POSTGRES_USER`) runs only the bootstrap.
- Why two: if the API connected as the owner, a compromised API process could alter or drop the tables, including the audit log. As `quorum_app` it can only do what the privileges below allow. The role names are fixed constants (not settings), so `db/schema.sql` does not depend on anyone's local `.env`.

## What the app role may and may not do
- It may `SELECT` and `INSERT` on every table the owner creates in schema `public`. This comes from default privileges set by [`db/bootstrap.sql`](../db/bootstrap.sql) (`ALTER DEFAULT PRIVILEGES FOR ROLE quorum_owner IN SCHEMA public`), so a new table gets it with no extra work, as long as the migration creates the table in `public`.
- It may `UPDATE` only the columns a migration grants explicitly, with a column-level grant such as `GRANT UPDATE (status) ON public.returns TO quorum_app`. It may never `DELETE` or `TRUNCATE`, and it cannot create objects in `public` or see the `migrations` schema.
- The test suite checks this as the real roles: `apps/api/tests/integration/test_roles.py` and `test_migrations.py`.

## Where each credential lives
Each password is stored once, in `.env` (copy it from [`.env.example`](../.env.example)). Compose builds the URLs it needs from the password variables, so you do not repeat a password in Docker. Native runs read two URLs from `.env`, which is where a password is repeated.

| Variable | Holds | Read by | Must match |
|---|---|---|---|
| `POSTGRES_PASSWORD` | the superuser's password | the postgres container (first start of a volume only) and the bootstrap | nothing else |
| `QUORUM_OWNER_PASSWORD` | the owner role's password | the bootstrap (creates the role); Compose builds the migrate service's own `OWNER_DATABASE_URL` from it | the password in `OWNER_DATABASE_URL`, and the role's real password |
| `QUORUM_APP_PASSWORD` | the app role's password | the bootstrap (creates the role); Compose builds the api service's `DATABASE_URL` from it | the password in `DATABASE_URL`, and the role's real password |
| `DATABASE_URL` | the app-role URL | the API and the tests, when run natively (Docker builds its own) | `QUORUM_APP_PASSWORD` |
| `OWNER_DATABASE_URL` | the owner-role URL | native `alembic` and the privilege tests | `QUORUM_OWNER_PASSWORD` |

- The API never sees the owner URL: it is not a field of `Settings`, and Compose does not pass it to the `api` service. `bash scripts/db-checks.sh config-check` fails if that ever changes.
- Use letters and digits only in both passwords. They are put into connection URLs without encoding, so `@`, `/`, `%` or `$` break the URL (an `@` makes the host read as `ss@postgres`) and the API then cannot connect: `/health` reports `db` as `error`.
- A value you have exported in your shell (for example after `set -a; . ./.env; set +a` for a native run) wins over `.env` for Compose. After editing `.env`, open a new shell or re-export.

## The bootstrap
- [`db/bootstrap.sql`](../db/bootstrap.sql) creates the two roles (`LOGIN NOSUPERUSER`), gives the owner `CREATE` on `public`, sets the default privileges above, and creates the `migrations` schema (owned by the owner, with no access for the app role) for Alembic's version table. It is idempotent: running it again changes nothing. It connects over the compose network, where passwords are checked (the official postgres image trusts the local socket and loopback). It fails, with a non-zero exit, if either password variable is missing or empty.
- It never changes the password of a role that already exists. Editing `.env` therefore does not change a role's real password: see "Changing a password" below.
- Roles are cluster-wide but grants and default privileges are per database, so for another database run `bash scripts/db-checks.sh bootstrap <dbname>`.
- In Compose, `bootstrap` and `migrate` are one-shot services that run before the API: `bootstrap`, then `migrate`, then `api`. `docker compose run --rm migrate` runs both, in that order, without starting the API.

## Adding a migration
Migrations are hand-written, forward-only and numbered in sequence.
1. Make sure the datastores are up and migrated: `docker compose up -d --wait postgres redis`, then `docker compose run --rm migrate`.
2. Create the revision with the next sequential id (Alembic otherwise generates a random one): `cd apps/api && uv run alembic revision --rev-id 0002 -m <slug>`. This needs no database. It writes `apps/api/migrations/versions/0002_<slug>.py` from the template.
3. Write the SQL by hand with `op.execute(...)` after `from alembic import op`, including the grants the new table needs (a column-level `UPDATE` grant if the app role must update it). There is no `downgrade`: a mistake is fixed by a new revision that moves forward (ADR-0002, Decision 1).
4. Apply it: `docker compose run --rm --build migrate` (`--build` makes Compose use your new file, not an older image), or natively with `set -a; . ./.env; set +a; cd apps/api && uv run alembic upgrade head`.
5. Regenerate the schema file: `bash scripts/db-checks.sh drift --update`. It needs the datastores up. Review the diff of [`db/schema.sql`](../db/schema.sql) and commit it together with the migration. CI runs `bash scripts/db-checks.sh drift` and fails if the file no longer matches.

## The drift check
- `bash scripts/db-checks.sh drift` creates a scratch database (`quorum_drift_<pid>`, never your real one), runs the bootstrap and every migration on it, dumps it with `pg_dump --schema-only` (owners and privileges kept) and compares the dump with `db/schema.sql`. The scratch database is dropped at the end, also when the run fails. `drift --update` writes the file instead of comparing it.
- It covers tables, columns, constraints, owners, grants, default privileges and schema ACLs. Its limits:
  - It sees no data. A migration that changes no schema object (like the baseline `0001`) is invisible to it, and so is the revision number in `migrations.alembic_version`.
  - The database-level `CONNECT` privilege is not in a schema-only dump, so changing it does not show as drift (checked: two databases that differ only in `CONNECT` dump identically).
  - A `kill -9` of the script can leave a `quorum_drift_<pid>` database behind, because the cleanup cannot run. It is harmless and can be dropped by hand. This follows from how the cleanup works and was not tested.
  - As a safety guard the script refuses to print or write a dump that contains the word `PASSWORD` or `SCRAM-SHA-256`. A column or table whose name contains `password` therefore stops `drift` (checked with a `password_hash` column). Rename it, or change the guard in `scripts/db-checks.sh` deliberately.
  - The `pg_dump` version is pinned with the Postgres image (the same pin in `docker-compose.yml` and CI). If a new pin changes the dump format, `drift` reports it as drift. That is the signal to run `drift --update` and review the diff.

## Changing a password
A role's password lives in the database, so changing `.env` is not enough, and the bootstrap will not change it. The procedure below changes it in the database first and then in `.env`. It was tried on a throwaway stack, for both roles. The new password is typed into the shell silently, handed to the container by name (so it is never on a command line), and sent to the server in a session that does not log statements (so it does not reach the server log, even if the statement fails). Use letters and digits only. Run the block in bash: `read -rs -p` is bash syntax, and in zsh `-p` means something else.

For the app role:

```bash
read -rs -p "New password (letters and digits only): " NEW_PASSWORD; echo
export NEW_PASSWORD
docker compose run --rm --no-deps -T -e NEW_PASSWORD --entrypoint psql bootstrap -X -v ON_ERROR_STOP=1 -v role=quorum_app <<'SQL'
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
\getenv new_pw NEW_PASSWORD
ALTER ROLE :"role" PASSWORD :'new_pw';
SQL
unset NEW_PASSWORD
```

For the owner role, run the same commands with `-v role=quorum_owner`.

Then, straight away:
1. Put the same value in `.env` with an editor: `QUORUM_APP_PASSWORD` and the password inside `DATABASE_URL` (owner: `QUORUM_OWNER_PASSWORD` and `OWNER_DATABASE_URL`).
2. Restart so the API picks it up: `docker compose up -d --wait`.

What the try showed:
- The old password no longer connects (`password authentication failed`) and the new one does. After the restart `/health` is `ok`, and `docker compose run --rm migrate` works with the new owner password.
- Between the `ALTER ROLE` and the restart the API answers 503, because its health check opens a new connection with the old password. Do the two steps back to back.
- Running the bootstrap again, even with the old values in its environment, changed neither password.
- The password did not appear in any command output, and not in the logs of any service. A failing `ALTER ROLE` is not logged either, but it is when the two `SET` lines are left out.
- While the one-off container exists its environment holds the value, so anyone who can run `docker inspect` on the Docker host could read it in that moment. `--rm` removes the container when the command ends.

## Troubleshooting
- `migrate` fails with `password authentication failed for user "quorum_owner"` (or the API reports `db` as `error`): the value in `.env` does not match the role's real password. An existing volume keeps the old password, because the bootstrap never changes it. Fix it with "Changing a password", setting the role's password to the value in `.env`. Wiping the volume (`docker compose down -v`) also fixes it but destroys the data, so only the maintainer does that.
- A dev volume from before this ticket has no roles and no `migrations` schema. Add `QUORUM_OWNER_PASSWORD` and `QUORUM_APP_PASSWORD` to `.env`, then run `docker compose run --rm migrate` once (not tested on a real pre-ticket volume; a volume with no roles is the same state as a fresh one, which was tested).
- `docker compose` says `set it in .env`: one of the two `QUORUM_*` password variables is missing or empty in `.env`.

## Not settled yet
What happens when the database is down at API startup is not decided: the P1-2 spike (check B3) failed one condition when it opened a connection pool while the database was down, and the data-access ticket decides it (ADR-0002, Decision 2).
