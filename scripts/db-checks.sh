#!/usr/bin/env bash
# Database checks for ADR-0002.
#
# Usage: bash scripts/db-checks.sh <command>
#   bootstrap [dbname]   run db/bootstrap.sql (roles and grants) against a database
#   idempotency          run the bootstrap twice and show that the second run changed nothing
#   config-check [file]  check the resolved Compose config (the committed .env.example, never your
#                        .env): the api service holds only the app-role URL, the migrate service
#                        holds only the owner URL, and api -> migrate -> bootstrap is the start
#                        order. Prints names, never values. Needs docker and python3, not a
#                        running stack.
#   drift                create a SCRATCH database (quorum_drift_<pid>, never POSTGRES_DB), run the
#                        bootstrap and `alembic upgrade head` on it, dump it with
#                        `pg_dump --schema-only` (owners and privileges kept) and diff the
#                        normalised dump against the committed db/schema.sql. The scratch database
#                        is always dropped. ADR-0002, Decision 1.
#   drift --update       the same, but write db/schema.sql instead of comparing. Review the diff of
#                        db/schema.sql and commit it.
#
# Where psql runs is chosen by ONE function, pg(), and nothing else differs:
#   DB_CHECKS_RUNNER=compose (default)  a one-off container of the compose `bootstrap` service,
#       on the compose network. No host networking, so it works with Docker Desktop on WSL.
#       Needs the compose project's postgres running. COMPOSE_PROJECT_NAME, COMPOSE_FILE and
#       COMPOSE_ENV_FILES pick another project.
#   DB_CHECKS_RUNNER=host  `docker run --network host postgres:17.11-alpine`, connecting to
#       localhost:$POSTGRES_HOST_PORT. Used by the CI api job, where the postgres service
#       container publishes its port on the runner. Reads POSTGRES_USER, POSTGRES_PASSWORD,
#       POSTGRES_DB, QUORUM_OWNER_PASSWORD and QUORUM_APP_PASSWORD from the environment.
#
# Passwords are never printed and never put on a command line.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."        # always run from the repo root (also when sourced)

RUNNER="${DB_CHECKS_RUNNER:-compose}"
PG_IMAGE="postgres:17.11-alpine"              # keep in step with docker-compose.yml and ci.yml

fail() { printf '\n❌ %s\n' "$*" >&2; exit 1; }
log()  { printf '==> %s\n' "$*"; }

case "$RUNNER" in
  compose) ;;
  host)
    # Exported so docker run can pass them by NAME (-e VAR): the values never reach argv.
    export PGHOST=localhost PGPORT="${POSTGRES_HOST_PORT:-5432}"
    export PGUSER="${POSTGRES_USER:?POSTGRES_USER is not set}"
    export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set}"
    export PGDATABASE="${POSTGRES_DB:?POSTGRES_DB is not set}"
    : "${QUORUM_OWNER_PASSWORD:?QUORUM_OWNER_PASSWORD is not set}"
    : "${QUORUM_APP_PASSWORD:?QUORUM_APP_PASSWORD is not set}"
    export QUORUM_OWNER_PASSWORD QUORUM_APP_PASSWORD
    ;;
  *) fail "DB_CHECKS_RUNNER must be 'compose' or 'host' (got '$RUNNER')" ;;
esac

# pg <tool> [args...]: run a postgres client tool (psql, later pg_dump). Reads stdin.
pg() {
  local tool="$1"; shift
  if [ "$RUNNER" = compose ]; then
    docker compose run --rm --no-deps -T --entrypoint "$tool" bootstrap "$@"
  else
    docker run --rm -i --network host --tmpfs /var/lib/postgresql/data \
      -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE \
      -e QUORUM_OWNER_PASSWORD -e QUORUM_APP_PASSWORD \
      -v "$PWD/db/bootstrap.sql:/bootstrap.sql:ro" \
      "$PG_IMAGE" "$tool" "$@"
  fi
}

run_bootstrap() {
  local args=()
  [ -n "${1:-}" ] && args=(-v "dbname=$1")
  pg psql -X -q -v ON_ERROR_STOP=1 "${args[@]}" -f /bootstrap.sql
}

# One line per fact: role attributes (including the password hash), memberships, default
# privileges, schema ACLs and the database ACL. Hashes are compared but never printed.
snapshot() {
  pg psql -X -At -F '|' -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'role', rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
       rolreplication, rolbypassrls, rolconnlimit, coalesce(rolvaliduntil::text, ''),
       coalesce(rolpassword, '')
  FROM pg_authid WHERE rolname IN ('quorum_owner', 'quorum_app') ORDER BY rolname;
SELECT 'member', roleid::regrole, member::regrole
  FROM pg_auth_members
 WHERE roleid::regrole::text IN ('quorum_owner', 'quorum_app')
    OR member::regrole::text IN ('quorum_owner', 'quorum_app') ORDER BY 2, 3;
SELECT 'defacl', defaclrole::regrole, defaclnamespace::regnamespace, defaclobjtype, defaclacl
  FROM pg_default_acl ORDER BY 2, 3, 4;
SELECT 'schema', nspname, nspowner::regrole, coalesce(nspacl::text, '')
  FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY nspname;
SELECT 'database', datname, datdba::regrole, coalesce(datacl::text, '')
  FROM pg_database WHERE datname = current_database();
SQL
}

cmd_idempotency() {
  local dir; dir="$(mktemp -d)"
  trap "rm -rf '$dir'" EXIT                     # expanded now: $dir is local to this function

  log "Bootstrap, run 1"
  run_bootstrap
  snapshot > "$dir/after-run-1"
  log "Bootstrap, run 2"
  run_bootstrap
  snapshot > "$dir/after-run-2"

  # Positive controls: an empty snapshot would also "show no difference".
  local rows roles hashes
  rows="$(wc -l < "$dir/after-run-1" | tr -d ' ')"
  roles="$(grep -c '^role|' "$dir/after-run-1" || true)"
  hashes="$(awk -F'|' '$1 == "role" && $12 != ""' "$dir/after-run-1" | wc -l | tr -d ' ')"
  echo "snapshot rows=$rows roles=$roles password hashes present=$hashes"
  [ "$roles" -eq 2 ] || fail "expected 2 roles in the snapshot, found $roles"
  [ "$hashes" -eq 2 ] || fail "expected 2 password hashes in the snapshot, found $hashes"

  if cmp -s "$dir/after-run-1" "$dir/after-run-2"; then
    echo "run 2 changed nothing: no difference"
  else
    echo "run 2 CHANGED these (kind|name, values not shown):"
    diff "$dir/after-run-1" "$dir/after-run-2" | grep '^[<>]' | cut -d'|' -f1,2 | sort -u
    fail "the bootstrap is not idempotent"
  fi
}

# The api service must hold the app-role URL and nothing that opens the owner or the superuser.
# The checks look at names and at the user part of the URL, not at passwords: in .env.example
# every placeholder password is the same word, so comparing values would prove nothing.
# An env_file on the api service needs no rule of its own: Compose merges it into `environment`
# in the resolved JSON, so its variables show up there and the variable-name rules catch them.
CONFIG_CHECK_PY='
import json, re, sys

cfg = json.load(sys.stdin)
services = cfg["services"]
api = services["api"]
env = api.get("environment") or {}
problems = []

URL = r"^postgresql://([^:@/]+):[^@/]+@postgres:5432/"

for name in env:
    if re.search(r"OWNER|SUPERUSER|^POSTGRES_(USER|PASSWORD)$|^PG(USER|PASSWORD)$", name):
        problems.append("api has the variable " + name)
url = env.get("DATABASE_URL", "")
m = re.match(URL, url)
if not m:
    problems.append("api DATABASE_URL is missing or not postgresql://<user>:<password>@postgres:5432/")
elif m.group(1) != "quorum_app":
    problems.append("api DATABASE_URL does not use the role quorum_app")
for name, value in env.items():
    if "quorum_owner" in str(value):
        problems.append("api variable " + name + " names the owner role")

# The migrate service holds the owner URL and nothing else.
migrate = services.get("migrate")
menv = {}
if migrate is None:
    problems.append("there is no migrate service")
else:
    menv = migrate.get("environment") or {}
    if "DATABASE_URL" in menv:
        problems.append("migrate has a DATABASE_URL")
    for name in sorted(set(menv) - {"OWNER_DATABASE_URL", "DATABASE_URL"}):
        problems.append("migrate has the variable " + name + " (only OWNER_DATABASE_URL is allowed)")
    owner_url = menv.get("OWNER_DATABASE_URL")
    mo = re.match(URL, owner_url) if owner_url else None
    if not mo:
        problems.append("migrate OWNER_DATABASE_URL is missing or not postgresql://<user>:<password>@postgres:5432/")
    elif mo.group(1) != "quorum_owner":
        problems.append("migrate OWNER_DATABASE_URL does not use the role quorum_owner")


# The chain: bootstrap, then migrate, then api. Each waits for the previous one to exit 0.
def waits_for(service, dependency):
    depends = (service or {}).get("depends_on") or {}
    return (depends.get(dependency) or {}).get("condition") == "service_completed_successfully"


if "bootstrap" not in services:
    problems.append("there is no bootstrap service")
if not waits_for(api, "migrate"):
    problems.append("api does not wait for migrate with service_completed_successfully")
if migrate is not None and not waits_for(migrate, "bootstrap"):
    problems.append("migrate does not wait for bootstrap with service_completed_successfully")

if problems:
    for p in problems:
        print("  FAIL: " + p)
    sys.exit(1)
print("  api variables: " + ", ".join(sorted(env)))
print("  api DATABASE_URL uses quorum_app; no owner or superuser variable")
print("  migrate variables: " + ", ".join(sorted(menv)))
print("  migrate OWNER_DATABASE_URL uses quorum_owner; no DATABASE_URL")
print("  chain: api waits for migrate, migrate waits for bootstrap (service_completed_successfully)")
'

cmd_config_check() {
  local file="${1:-docker-compose.yml}"
  [ -f "$file" ] || fail "no such compose file: $file"
  [ -f .env.example ] || fail ".env.example not found"
  # Explicit file and env file, so a COMPOSE_* variable or your own .env cannot change the result.
  env -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
    docker compose --env-file .env.example -f "$file" config --format json \
    | python3 -I -c "$CONFIG_CHECK_PY" \
    || fail "the api or migrate service does not match the expected credentials or order (see above)"
  log "config-check passed for $file"
}

# ---------- drift: db/schema.sql against what the migrations produce (ADR-0002, Decision 1) ----------

SCHEMA_FILE="db/schema.sql"
# Pinned, so pg_dump's \restrict / \unrestrict lines are the same on every run. Alphanumeric only.
DRIFT_RESTRICT_KEY="quorumdriftcheck"

# alembic_upgrade <db>: `alembic upgrade head` as the owner, against database <db>. Like pg(), this is
# the only place that knows where it runs. The URL is the existing OWNER_DATABASE_URL with the
# database name replaced, built where the password already is, so it never reaches a command line.
alembic_upgrade() {
  local db="$1"
  if [ "$RUNNER" = compose ]; then
    # --build: the migrate service runs the migrations baked into its image, so rebuild (a cache
    # hit in seconds) to be sure the working tree is what is checked.
    docker compose run --rm --no-deps -T --build -e "DRIFT_DB=$db" --entrypoint sh migrate \
      -c 'OWNER_DATABASE_URL="${OWNER_DATABASE_URL%/*}/$DRIFT_DB" exec alembic upgrade head'
  else
    : "${OWNER_DATABASE_URL:?OWNER_DATABASE_URL is not set (the host runner runs alembic from apps/api)}"
    ( cd apps/api && OWNER_DATABASE_URL="${OWNER_DATABASE_URL%/*}/$db" uv run alembic upgrade head )
  fi
}

# drop_scratch <name>: drop a scratch database, but only one that is clearly ours. It refuses a name
# outside quorum_drift_<number>, POSTGRES_DB, and the database the runner is connected to.
drop_scratch() {
  local name="${1:-}"
  if ! [[ "$name" =~ ^quorum_drift_[0-9]+$ ]]; then
    echo "refusing to drop '$name': not a quorum_drift_<number> database" >&2
    return 1
  fi
  if [ "$name" = "${POSTGRES_DB:-}" ]; then
    echo "refusing to drop '$name': it is POSTGRES_DB" >&2
    return 1
  fi
  local current
  current="$(pg psql -X -At -c 'select current_database()')" || return 1
  if [ "$name" = "$current" ]; then
    echo "refusing to drop '$name': it is the database the runner is connected to" >&2
    return 1
  fi
  pg psql -X -q -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$name\" WITH (FORCE)"
}

# The only changes made to the dump. Everything else (owners, grants, default privileges, SET lines,
# the pinned \restrict key) is kept exactly as pg_dump wrote it.
#  1. the two "Dumped from/by ... version" comments: they carry the minor version, so bumping the
#     image pin would otherwise fail the check without any schema change;
#  2. trailing whitespace on a line, and
#  3. trailing blank lines, ending with exactly one newline: the trailing-whitespace and
#     end-of-file-fixer hooks would otherwise rewrite db/schema.sql and the check would never settle.
normalise_dump() {
  python3 -I -c '
import re, sys

lines = [line.rstrip() for line in sys.stdin.read().splitlines()]
lines = [l for l in lines if not re.match(r"-- Dumped (from database version|by pg_dump version) ", l)]
sys.stdout.write("\n".join(lines).rstrip("\n") + "\n")
'
}

# The header is part of the compared text, on both sides (not only written by --update).
schema_header() {
  cat <<'EOF'
-- db/schema.sql: the schema that the migrations and db/bootstrap.sql must produce (ADR-0002).
-- GENERATED by `bash scripts/db-checks.sh drift --update`. Do not edit by hand.
-- It is `pg_dump --schema-only` of an empty database after the bootstrap and `alembic upgrade head`,
-- with owners and privileges kept. Only the two "Dumped from/by" version comments are dropped.
-- `bash scripts/db-checks.sh drift` (CI, api job) fails when this file no longer matches.

EOF
}

# A schema-only dump of one database holds no role data. If a password or hash shows up anyway,
# neither print nor write it.
contains_credentials() { grep -q -i -E 'PASSWORD|SCRAM-SHA-256' "$1"; }

cleanup_drift() {
  local rc=$?
  drop_scratch "$1" || echo "WARNING: the scratch database $1 was not dropped" >&2
  rm -rf "$2"
  exit "$rc"
}

cmd_drift() {
  local update=0
  case "${1:-}" in
    "") ;;
    --update) update=1 ;;
    *) fail "usage: bash scripts/db-checks.sh drift [--update]" ;;
  esac
  if [ "$update" -eq 0 ] && [ ! -f "$SCHEMA_FILE" ]; then
    fail "$SCHEMA_FILE does not exist. Run: bash scripts/db-checks.sh drift --update, review it, and commit it"
  fi

  local scratch="quorum_drift_$$" dir
  dir="$(mktemp -d)"
  # Registered BEFORE the database exists, so it is dropped on success, on failure and on Ctrl-C.
  trap "cleanup_drift '$scratch' '$dir'" EXIT
  trap 'exit 130' INT TERM

  log "Creating the scratch database $scratch"
  pg psql -X -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$scratch\""
  log "Bootstrap on $scratch"
  run_bootstrap "$scratch"
  log "alembic upgrade head on $scratch"
  alembic_upgrade "$scratch"
  log "pg_dump --schema-only on $scratch (owners and privileges kept)"
  pg pg_dump -d "$scratch" --schema-only "--restrict-key=$DRIFT_RESTRICT_KEY" > "$dir/raw.sql"

  { schema_header; normalise_dump < "$dir/raw.sql"; } > "$dir/actual.sql"
  # Positive controls: an empty or stripped dump would also "have no drift".
  grep -q 'OWNER TO quorum_owner' "$dir/actual.sql" \
    || fail "the dump has no 'OWNER TO quorum_owner': were owners left out of the dump?"
  grep -q 'ALTER DEFAULT PRIVILEGES' "$dir/actual.sql" \
    || fail "the dump has no 'ALTER DEFAULT PRIVILEGES': were privileges left out of the dump?"
  if contains_credentials "$dir/actual.sql"; then
    fail "the dump contains a password or a hash: refusing to print or write it"
  fi

  if [ "$update" -eq 1 ]; then
    cp "$dir/actual.sql" "$SCHEMA_FILE.tmp" && mv "$SCHEMA_FILE.tmp" "$SCHEMA_FILE"
    log "wrote $SCHEMA_FILE ($(wc -l < "$SCHEMA_FILE" | tr -d ' ') lines). Review its diff and commit it."
    return 0
  fi

  if cmp -s "$SCHEMA_FILE" "$dir/actual.sql"; then
    log "no drift: $SCHEMA_FILE matches the schema the migrations produce"
    return 0
  fi
  diff -u --label "$SCHEMA_FILE (committed)" --label "expected (what the migrations produce)" \
    "$SCHEMA_FILE" "$dir/actual.sql" > "$dir/diff.txt" || true
  if contains_credentials "$dir/diff.txt"; then
    fail "drift detected, but the diff would show a password or a hash, so it is not printed"
  fi
  local total; total="$(wc -l < "$dir/diff.txt" | tr -d ' ')"
  echo "drift detected: $SCHEMA_FILE does not match what the migrations and db/bootstrap.sql produce."
  if [ "$total" -gt 60 ]; then
    echo "(unified diff, showing the first 60 of $total lines)"
  else
    echo "(unified diff, $total lines)"
  fi
  head -n 60 "$dir/diff.txt"
  echo
  echo "If this change is intended: run  bash scripts/db-checks.sh drift --update"
  echo "review the diff of $SCHEMA_FILE, and commit it."
  fail "schema drift"
}

main() {
  case "${1:-}" in
    bootstrap)    run_bootstrap "${2:-}" ;;
    idempotency)  cmd_idempotency ;;
    config-check) cmd_config_check "${2:-}" ;;
    drift)        cmd_drift "${2:-}" ;;
    *) fail "usage: bash scripts/db-checks.sh bootstrap [dbname] | idempotency | config-check [file] | drift [--update]" ;;
  esac
}

# Run only when executed, so the functions can be sourced and tested (the drop_scratch guard).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
