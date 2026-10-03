# Quorum

[![CI](https://github.com/mdumar99/quorum/actions/workflows/ci.yml/badge.svg)](https://github.com/mdumar99/quorum/actions/workflows/ci.yml)

## Overview
A governed multi-agent AI system for reviewing e-commerce returns and refunds. A panel of specialized agents evaluates each case together, with human review, a full audit trail, and a kill switch keeping the system accountable as it earns trust.

## Status
**Phase 0 complete** ([v0.1.0](https://github.com/mdumar99/quorum/releases/tag/v0.1.0)): the full stack, `/health`, CI and smoke test. No agents yet.
**Next:** Phase 1, the core data model and a first single-agent pipeline.

## Architecture
Next.js (`apps/web`) fetches health status server-side from a FastAPI backend (`apps/api`), which in turn talks to PostgreSQL and Redis. All four services run together under Docker Compose.

### Architecture Decisions
Decisions that are expensive to reverse are recorded as ADRs in [`docs/adr/`](docs/adr/README.md):

- [ADR-0001: Stack and architecture for Phase 0](docs/adr/0001-stack-choice.md)

## Setup
### Environment configuration
All configuration comes from environment variables — no connection details or environment-specific values are hardcoded, so the same code runs correctly on your laptop, in CI, and in Docker just by changing what's in the environment.

| File | Used by | When | Committed? |
|---|---|---|---|
| `.env` (from `.env.example`) | Compose + API | Native runs: read directly by `config.py`. Docker: read by Compose on the host, which passes values into containers via `environment:` | No |
| `apps/web/.env.local` (from `.env.local.example`) | Next.js | Native `npm run dev` only | No |
| `*.example` files | Humans | Reference — copy from these to create the real files above | Yes |

**Why Next.js has its own file:** Next.js only reads `.env*` files from its own package directory (`apps/web/`), not the repo root. The root `.env` is invisible to it, so `apps/web/.env.local` exists to give the frontend its own env file for the values it needs.

**Native vs Docker:** Running natively, `DATABASE_URL` and `REDIS_URL` point at `localhost` plus whichever port Compose publishes to the host, since that's the only way a process running directly on your machine can reach the containers. Inside Docker, the API container can't reach `postgres` or `redis` via `localhost` — that would mean the container itself — so `docker-compose.yml` overrides both variables per service with the container's own service name instead:

```yaml
services:
  api:
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
```

Same `.env` file, same variable names — Compose just substitutes different values for the pieces that only make sense inside its own network.

Note the port is `5432`, not `POSTGRES_HOST_PORT`: containers talk to Postgres on its internal port inside the Docker network; the host port only matters for processes outside it.

**Quick start:**
```bash
cp .env.example .env
cp apps/web/.env.local.example apps/web/.env.local
```

### Developer tooling
Install the git hooks once after cloning. They format and check every commit:

    uv tool install pre-commit
    pre-commit install

## Running Locally

### Option 1: Everything in Docker (recommended)
Brings up all four services — Postgres, Redis, the API, and the web app — in one command: the same stack the smoke test (`scripts/smoke-test.sh`) runs, and as close as this project gets to a production topology.

```bash
cp .env.example .env
docker compose up -d --build --wait
```

Open the web app at [http://localhost:3000](http://localhost:3000), and check the API directly at [http://localhost:8000/health](http://localhost:8000/health).

Useful commands:
```bash
docker compose ps                # see what's running and its health
docker compose logs -f api       # tail one service's logs
docker compose down              # stop, keep data
docker compose down -v           # stop and WIPE data
```

### Option 2: Native dev loop (hot reload)
Use this while actively coding on the API or the frontend — both `uvicorn --reload` and `next dev` pick up file changes instantly, which Option 1's built container images don't. Needs both env files from [Setup → Quick start](#environment-configuration) — the web app reads `apps/web/.env.local` directly, and native `npm run dev` doesn't get it from Compose.

```bash
docker compose up -d --wait postgres redis                               # datastores only

# Terminal 1 (from the repo root)
cd apps/api && uv run uvicorn app.main:app --reload --no-access-log

# Terminal 2 (from the repo root)
cd apps/web && npm run dev
```

Don't run both options at once — they both want ports 8000 and 3000, and the second one to start will fail to bind.

## Testing
### Running tests locally

**API:**
```bash
cd apps/api && uv run pytest
```
Some tests are marked `integration` and need real Postgres/Redis running (`docker compose up -d --wait postgres redis` first). To run only the tests that don't need them:
```bash
cd apps/api && uv run pytest -m "not integration"
```

**Web:**
```bash
cd apps/web && npm test
```

### What CI runs on every PR
- **Hooks:** every pre-commit hook runs on every file, so skipping them locally with `--no-verify` doesn't get anything past CI
- **Lint:** `ruff check` for the API and `eslint` for the web app, catching style and correctness issues static analysis can find without running anything
- **Format:** `ruff format --check` and `prettier --check`, confirming every file matches the formatting pre-commit already enforces locally
- **Type check:** `tsc --noEmit` on the web app, catching type errors that ESLint doesn't
- **Test:** the full API and web test suites, with Postgres and Redis running as real service containers so the integration tests run in CI too, not just locally
- **Build:** `next build` for the web app (with no API running, proving nothing is fetched at build time), plus a Docker build of both images

### Smoke test
```bash
docker compose down
bash scripts/smoke-test.sh
```
Needs `.env` to exist. Stop the dev stack first — the smoke test shares host ports with it, and the script detects a conflict and tells you if you forget.

It brings up the full stack and checks that all services come up healthy, that `/health` returns the exact expected contract, and that the page renders in the `healthy` state. It runs as its own Compose project (`quorum-smoke`), so its teardown never touches your dev data.

It runs on every PR and is a required check.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions: branches, commits, PRs, and tooling.

## Troubleshooting

### Port 5432 already in use: `docker compose up` fails ("Ports are not available … 5432 …") or the smoke test reports "port 5432 is already in use"

**Cause:** something else on your machine is already listening on port 5432 — usually a PostgreSQL instance installed directly on Windows (not in Docker), running as a background service that starts automatically. Docker's `postgres` container is trying to bind to the same port on the host and losing.

**Fix, option 1 (keep both, change the port Compose uses):**
1. In `.env`, add or set `POSTGRES_HOST_PORT=5433`.
2. In the same file, change the port in `DATABASE_URL` to match: `…@localhost:5433/…`. (Postgres still listens on 5432 inside the container; only the host-side port changes.)
3. Restart: `docker compose down && docker compose up -d --wait`

**Fix, option 2 (stop the conflicting service):**

If you don't need the Windows-native Postgres running, stop it instead:

- Open **Services** (Win+R → `services.msc`), find `postgresql-x64-...`, right-click → **Stop**. Set it to **Manual** startup if you don't want it competing for the port again on every reboot.

Either fix works — option 1 is faster if you don't want to touch anything outside this project; option 2 is cleaner if you don't actually use the Windows-native Postgres install for anything else.

## License
MIT — see [LICENSE](LICENSE).
