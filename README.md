# Quorum

## Overview
A governed multi-agent AI system for reviewing e-commerce returns and refunds. A panel of specialized agents evaluates each case together, with human review, a full audit trail, and a kill switch keeping the system accountable as it earns trust.


## Architecture
Next.js (`apps/web`) fetches health status server-side from a FastAPI backend (`apps/api`), which in turn talks to PostgreSQL and Redis. All four services run together under Docker Compose.

### Architecture Decisions
Decisions that are expensive to reverse are recorded as ADRs in [`docs/adr/`](docs/adr/README.md):

- [ADR-0001: Stack and architecture for Phase 0](docs/adr/0001-stack-choice.md)

## Setup
_Filled in by P0-5 (#5) and P0-7 (#7)._

## Running Locally
_Filled in by P0-2 (#2)._

## Testing
_Filled in by P0-6 (#6) and P0-9 (#9)._

## Contributing
_Filled in by P0-7 (#7)._

## Troubleshooting

### `docker compose up` fails: "Ports are not available … 5432 … forbidden by its access permissions"

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

