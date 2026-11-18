# Data access: the connection pool

How the API talks to PostgreSQL: the pool, its settings, what happens when the database is down, what was measured, and what is not covered. The decision behind it is [ADR-0002](adr/0002-migrations-data-access-roles.md), Decision 2.

## What this is

- **One pool**, from psycopg 3 and `psycopg_pool` (`AsyncConnectionPool`). There is no ORM; rows are mapped into Pydantic models with `class_row`. The code is in `apps/api/app/db/pool.py`, `apps/api/app/db/returns.py` and `apps/api/app/db/models.py`.
- **The pool connects as the app role.** It uses `DATABASE_URL` and never the owner URL (see [database-roles.md](database-roles.md)).
- **`/health` keeps its own connection.** It opens one connection per check (`apps/api/app/db/session.py`) and does not use the pool, so its contract is unchanged.
- **A connection is never held across the LLM call.** A connection is only used inside a short `async with pool.connection()` block (ADR-0002, Decision 2). No code path uses the pool together with an LLM call today.
- **Time comes from the database.** In `returns`, the columns `as_of`, `created_at` and `updated_at` are each `timestamp with time zone DEFAULT now() NOT NULL`, so a row gets its times from the Postgres clock when it is inserted (`now()` is the start time of the transaction). `audit_log.created_at` has the same default. The schema has no trigger that refreshes `updated_at`: it is only a default, and the app role may update that column (`GRANT UPDATE(updated_at)`).

## The settings

All three are optional environment variables. `.env.example` lists them, commented out, with these defaults.

| Setting | Default | Meaning |
|---|---|---|
| `DB_POOL_MIN_SIZE` | 4 | The number of connections the pool opens and keeps. At least 1. |
| `DB_POOL_MAX_SIZE` | 4 | The largest number of connections the pool will hold. At least 1. With the same value as the minimum, the pool never grows. |
| `DB_POOL_CHECKOUT_TIMEOUT_SECONDS` | 5.0 | How long a request waits for a free connection, in seconds, before it fails with `PoolTimeout`. Greater than 0 and finite. Set below the LLM call timeout (ADR-0004, Decision 1). |

`DB_POOL_MIN_SIZE` must not be above `DB_POOL_MAX_SIZE`. A configuration that breaks the rule stops the API at start; the error says `db_pool_min_size (N) must not be above db_pool_max_size (M)`.

`docker-compose.yml` passes none of these settings to the api service, so the defaults apply in Compose. To change one there, add it to the api service's `environment`.

These defaults are the configuration that was measured (see [Measured](#measured)). Only 4 / 4 was measured with the fixed Postgres address; other sizes were not.

## Start and stop

- **The pool opens without waiting** (`open(wait=False)`). The API starts and listens even if Postgres is down; the pool's background workers keep trying to connect.
- **The log event `db_pool_started`** is written once at start, as one JSON line, with the fields `min_size`, `max_size` and `checkout_timeout_seconds`. It is logged before `startup_complete`. It never contains the URL.
- **The `DATABASE_URL` check is syntax only.** A string that is not a connection string stops the boot with `DatabaseUrlError`, whose message is always `DATABASE_URL is not a valid connection string`. It never contains the URL, and it carries no chained error that might. A wrong host, port or `sslmode` still parses: the API boots and the first use fails, which looks like a database that is down.
- **At shutdown the pool is closed with a 3 s timeout**, below Docker's 10 s stop grace, so a database that is down cannot hang a stop.

## Using the pool

```python
pool = get_pool(request)
async with pool.connection() as conn:
    row = await get_return(conn, return_id)
```

- `get_pool(request)` returns the pool kept on `app.state`, for a FastAPI dependency.
- `get_return(conn, return_id)` returns a `ReturnRow`, or `None` for an unknown id. It takes a connection: the caller owns the `async with` block, so it also owns the transaction (the block commits when it ends normally and rolls back on an error) and can run several statements in one.
- `ReturnRow` is a frozen Pydantic model with one field per column of `returns`. The query lists its columns explicitly and never uses `SELECT *`, so a schema change fails loudly instead of silently adding or losing a field.
- **Errors are not mapped here.** Turning `PoolTimeout` (and a connection error) into an HTTP response is the returns API's job (P1-12).

## Measured

All runs used Docker Desktop on WSL2 and Docker Compose v2.32.4, the pool at 4 / 4, one API worker (as in the Dockerfile), and the host name `postgres` kept in `DATABASE_URL` and resolved through `extra_hosts` to a fixed address (see [The Compose network](#the-compose-network)). Every run used a throwaway Compose project on its own subnet; the default subnet itself was not run. The runs fall into two groups, and every row says which one it is from.

### (a) The shipped `docker-compose.yml`

The repository's `docker-compose.yml` as shipped, with the subnet prefix set to a throwaway subnet and the published ports moved (a running dev stack uses the defaults), and the API image built from the final code. "Without the pool" means the same Compose with the API image from before the pool existed.

| What was measured | Result |
|---|---|
| Listen: time until the API listens, Postgres stopped before the start, 5 boots with 60 `/health` calls each | 1.090, 1.102, 1.117, 1.236 and 3.810 s; median 1.117 s. |
| Listen, 20 boots with the pool and no requests (two sets, each interleaved with the no-pool runs below) | First set: 0.988 to 3.754 s, median 1.081 s. Second set: 0.977 to 1.110 s, median 0.992 s. |
| Listen, 20 boots without the pool (the same two sets) | First set: 0.971 to 2.093 s, median 1.098 s. Second set: 0.977 to 3.875 s, median 1.006 s. |
| The start-up stall | In 3 of the 85 boots above (45 with the pool, 40 without) the start took about 2.7 s longer: 3.810 s and 3.754 s with the pool, 3.875 s without it. In those boots the time passed before uvicorn logged `Started server process` (3.761, 3.679 and 3.803 s after the container started, against medians of 0.97 to 1.01 s in the four 20-boot sets), that is, during the container start, the Python start or the import of the app. One more boot without the pool listened at 2.093 s, but its server had started at 1.043 s. The import of the app alone took 0.71 to 0.89 s in 40 standalone runs per image (no Compose, no network). The cause was not found. |
| `/health` with Postgres down | 300 of 300 calls (60 on each of the 5 boots) answered 503 with `db` "error" and `redis` "ok". The slowest took 2.004 s (the check's own 2 s limit). |
| `/health` with Postgres stopped under a running API | 80 of 80 calls (60, then 20 after a 60 s wait) answered 503 with `db` "error" and `redis` "ok". The slowest took 2.003 s. |
| `docker compose stop api` with Postgres down | Exit code 0 every time. With `/health` calls: 0.5 to 0.65 s. Stopped right after it started listening, with no requests: 2.14 to 2.31 s with the pool (40 boots), against 0.46 to 0.66 s without it (40 boots). This was not investigated; an untested guess is the 2 s connection timeout plus overhead. |

All 20 boots of each set, sorted, in seconds:

- With the pool, first set: 0.988, 0.989, 0.990, 0.992, 0.997, 0.999, 1.005, 1.006, 1.080, 1.081, 1.081, 1.084, 1.086, 1.086, 1.088, 1.088, 1.090, 1.096, 1.100, 3.754.
- With the pool, second set: 0.977, 0.980, 0.982, 0.985, 0.986, 0.986, 0.987, 0.987, 0.989, 0.991, 0.992, 0.992, 0.993, 0.994, 0.998, 1.004, 1.031, 1.101, 1.107, 1.110.
- Without the pool, first set: 0.971, 0.981, 0.992, 1.008, 1.064, 1.085, 1.086, 1.092, 1.094, 1.097, 1.098, 1.098, 1.102, 1.103, 1.103, 1.105, 1.109, 1.118, 1.184, 2.093.
- Without the pool, second set: 0.977, 0.978, 0.981, 0.983, 0.986, 0.988, 0.991, 0.994, 0.994, 1.003, 1.009, 1.013, 1.076, 1.079, 1.083, 1.084, 1.092, 1.099, 1.104, 3.875.

### (b) The earlier scratch override

The same design, with the fixed address, `extra_hosts` and subnet set in a scratch Compose override file instead of the shipped file, and the pool at the same 4 / 4 defaults.

| What was measured | Result |
|---|---|
| Listen, Postgres stopped before the start | Median 0.981 s over 5 boots (0.965 to 0.992 s). Over a separate 20 boots: 0.965 to 2.127 s, median 1.092 s. The same code without a pool: 0.979 to 2.134 s, median 1.075 s, over the same 20 boots. None of the 60 boots of that comparison (20 each without the pool, with the address in `DATABASE_URL`, and with `extra_hosts`) took more than 2.134 s. |
| `/health` with Postgres down, 60 calls on each of 5 boots | 300 of 300 answered 503 with `db` "error" and `redis` "ok". The slowest took 2.004 s (the check's own 2 s limit). |
| `/health` while checkouts fail (60 s, about 5 checkouts a second) | 42 of 42 calls answered 503 with `db` "error" and `redis` "ok", the slowest 2.003 s. |
| Checkouts with Postgres down | 300 of 300 failed with `PoolTimeout` after 4.9993 to 5.002 s. |
| Postgres down for 330 s, then back | `/health` answered 503 with `db` "error" and `redis` "ok" in all 12 samples (one about every 30 s). The pool's connection slots gave up after 300 s (the library's default). The first checkout after Postgres was healthy again succeeded at once (0.01 s), and so did the 89 after it; `/health` answered 200 within 0.02 s. |
| Restart with a full, idle pool | Postgres stopped for 5 s and started again, then one request a second for 30 s: the first 4 failed at once with `psycopg.errors.AdminShutdown`, and the other 26 succeeded. |
| `docker compose stop api` with Postgres down | Finished by itself, exit code 0, in 0.5 to 0.65 s in the runs with `/health` calls or checkouts. Stopped right after it started listening, with no requests (20 boots), it took 2.09 to 2.26 s, against 0.51 to 0.6 s for the same code without a pool. This was not investigated; the pool closes with a 3 s timeout. |

**Compared with keeping the name resolved by Docker's DNS** (group (b) runs, scratch override, `postgres` resolved by Docker's DNS instead of `extra_hosts`; 3 boots): the API listened after 9.1 s (9.1 to 9.3 s), and the Redis check failed in 87 of 180 `/health` calls. While checkouts failed, only 7 of 33 `/health` calls were clean, and Redis was not "ok" in 26. Neither a smaller pool nor a later open fixed it. With a minimum of 2 and a maximum of 4, the API listened after 9.3, 4.7 and 10.3 s on 3 boots, and 139 of 180 `/health` calls were clean (Redis not "ok" in the other 41). With a minimum of 1 and a maximum of 4, it listened normally (0.98 s on each of 3 boots), but only 156 of 180 `/health` calls were clean (Redis not "ok" in the other 24). Opening the pool at the first checkout, instead of at the start, left only 6 of 32 `/health` calls clean while checkouts failed.

**Cause (the failed lookup was seen in an earlier test; the thread starvation is an inference):** when the Postgres container is stopped, Docker's DNS stops resolving its name. In an earlier test with the same design (the host name resolved by Docker's DNS), the pool's own warnings said it could not resolve the host name while the container was stopped: `failed to resolve host 'postgres': [Errno -2] Name or service not known`. Each failed lookup is slow (`apps/api/app/routers/health.py` documents lookups that hold a thread for about 8 s). The rest is an inference supported by the measurements, not measured directly: the pool's background attempts use up the event loop's small pool of resolver threads, which delays the start and the Redis check (Redis is also reached by name). What supports it: the runs that kept the name but needed no lookup were clean, and the runs that changed only the pool size or when the pool opens were not.

## The Compose network

- **Postgres gets a fixed address** on the project's default network, and **the api maps the name `postgres` to it with `extra_hosts`**, so the pool needs no DNS lookup. `DATABASE_URL` keeps the host name `postgres` and port 5432; only the name resolution changes.
- **`QUORUM_NET_PREFIX`** sets the subnet: the first three numbers of a `/24`, `172.31.77` by default (Postgres is the address ending in `.10`). Set it in `.env` to change all three entries together. Change it if the subnet clashes with another network on your machine, a VPN for example.
- **On an existing stack, run `docker compose down` once before the next `docker compose up`.** The first `up` after the fixed-subnet change creates a new default network and then stops with `Error response from daemon: network <id> not found` while it starts the other services, leaving only Postgres running. `docker compose down` (never with `-v`, which wipes the database volume) removes the containers and the network; the next `docker compose up -d --build --wait` then brings the stack up healthy on the new subnet. The database volume is kept: the existing data was reused and no migration ran again. This was seen once, on one existing dev stack. Fresh clones and CI are not affected.
- **Not measured outside Compose.** The default subnet itself was not measured either; the runs used a different one with the same design.

## Known limits

- **After a Postgres restart or kill, requests fail until the pool has replaced the dead idle connections.** The first requests that receive a dead connection fail at once with `psycopg.errors.AdminShutdown`, up to the pool size (4 here), and then the pool has replaced them. Measured: with a full, idle pool, Postgres stopped for 5 s and started again, then one request a second for 30 s: the first 4 failed with `AdminShutdown` and the other 26 succeeded.
- **A connection that dies while it is in use is not handled.** The error reaches the caller.
- **`psycopg_pool`'s own `check=` option was tried and is not used.** It tests a connection when it is handed out. With `psycopg_pool` 3.3.3, a single request that meets 4 dead idle connections waited the full 5 s and failed with `PoolTimeout` (3 of 3 runs): the library sleeps about 0, 1 and 2 s between checks of the dead connections, which sit ahead of the new ones in the queue, and never reaches a new one.
- **Pool warnings are not formatted by the app.** Messages such as `error connecting in ...` and `reconnection attempt in pool ... failed` are written to the standard-library logger `psycopg.pool`, which the app does not configure (the app's JSON logging is `structlog` only, and the default logging configuration of uvicorn 0.54.0 sets up only the `uvicorn`, `uvicorn.error` and `uvicorn.access` loggers and has no entry for the root logger). With nothing configured, Python's last-resort handler writes records of level WARNING and above to stderr as the bare message: no timestamp, no level, not JSON. Records below WARNING, such as the pool's INFO lines, are dropped. This is what the shipped image logged with Postgres stopped: `error connecting in 'quorum-api': connection timeout expired`. The line holds only the pool name and the error, with no host, no port and no password. It appears in `docker compose logs api` among the JSON lines; with `--timestamps` it follows Docker's own timestamp, and the app adds none. There were 28 such lines on each of the 5 boots measured. With the fixed address, a stopped Postgres ends each attempt with `connection timeout expired`. Other errors can name the host and port: in a test with a real pool against a closed port, a refused connection did.
