#!/usr/bin/env bash
# Database checks for ADR-0002.
#
# Usage: bash scripts/db-checks.sh <command>
#   bootstrap [dbname]   run db/bootstrap.sql (roles and grants) against a database
#   idempotency          run the bootstrap twice and show that the second run changed nothing
#   config-check [file]  check that the api service of the resolved Compose config (the committed
#                        .env.example, never your .env) holds only the app-role URL. Prints
#                        no values. Needs docker and python3, not a running stack.
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

cd "$(dirname "$0")/.."                       # always run from the repo root

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
api = cfg["services"]["api"]
env = api.get("environment") or {}
problems = []

for name in env:
    if re.search(r"OWNER|SUPERUSER|^POSTGRES_(USER|PASSWORD)$|^PG(USER|PASSWORD)$", name):
        problems.append("api has the variable " + name)
url = env.get("DATABASE_URL", "")
m = re.match(r"^postgresql://([^:@/]+):[^@/]+@postgres:5432/", url)
if not m:
    problems.append("api DATABASE_URL is missing or not postgresql://<user>:<password>@postgres:5432/")
elif m.group(1) != "quorum_app":
    problems.append("api DATABASE_URL does not use the role quorum_app")
for name, value in env.items():
    if "quorum_owner" in str(value):
        problems.append("api variable " + name + " names the owner role")

if problems:
    for p in problems:
        print("  FAIL: " + p)
    sys.exit(1)
print("  api variables: " + ", ".join(sorted(env)))
print("  api DATABASE_URL uses quorum_app; no owner or superuser variable")
'

cmd_config_check() {
  local file="${1:-docker-compose.yml}"
  [ -f "$file" ] || fail "no such compose file: $file"
  [ -f .env.example ] || fail ".env.example not found"
  # Explicit file and env file, so a COMPOSE_* variable or your own .env cannot change the result.
  env -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
    docker compose --env-file .env.example -f "$file" config --format json \
    | python3 -I -c "$CONFIG_CHECK_PY" \
    || fail "the api service holds more than the app-role URL (see above)"
  log "config-check passed for $file"
}

case "${1:-}" in
  bootstrap)    run_bootstrap "${2:-}" ;;
  idempotency)  cmd_idempotency ;;
  config-check) cmd_config_check "${2:-}" ;;
  *) fail "usage: bash scripts/db-checks.sh bootstrap [dbname] | idempotency | config-check [file]" ;;
esac
