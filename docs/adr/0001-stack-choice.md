# ADR-0001: Stack and architecture for Phase 0

- **Status:** Accepted
- **Date:** 2026-09-26
- **Deciders:** mdumar99

## Context
Quorum is a governed multi-agent AI system for reviewing e-commerce returns and refunds. Eventually it needs to run several specialized agents per case (policy, risk, image, behavior), route uncertain or high-risk decisions to a human reviewer, and record every decision in a tamper-evident audit trail — with a global kill switch to shut automation off entirely if something goes wrong.

None of that logic exists yet. Phase 0's job is narrower: prove that a backend, a frontend, and their two datastores can run together, talk to each other correctly, and be verified by an automated test — nothing more. But the stack chosen here is the foundation the agent pipeline, the audit log, and the governance gate all get built on top of in later phases, so it's worth deciding deliberately now rather than drifting into it file by file.

## Decision

### 1. Stack: FastAPI + Next.js + PostgreSQL + Redis

**FastAPI (Python).** The agent pipeline is the core of this product, and the Python ecosystem is where the agent and LLM tooling actually lives — LangGraph, LangChain, the various model SDKs, and the surrounding RAG/eval tooling are Python-first, with other languages trailing behind. Choosing a different backend language would mean either fighting that ecosystem or maintaining a second service just to run the agents. FastAPI specifically gives async support (needed once the agent pipeline is doing concurrent I/O against LLM APIs, Postgres, and Redis), automatic request/response validation via Pydantic, and OpenAPI docs for free.

**Next.js.** The frontend needs to talk to an API that may not be reachable from the browser (containerized backend, private network, etc.). This ADR adopts the pattern that the browser doesn't call the API directly — the Next.js server does, and the browser only ever talks to Next.js. A plain React SPA can't do this: it has no server tier of its own, so it would have to call the API directly from the browser, which means the API must be publicly reachable and CORS-configured from day one. Next.js's server-side data fetching (Server Components) avoids that entirely for Phase 0, and gives the option to add real server-rendered pages (the reviewer dashboard, in a later phase) without switching frameworks.

**PostgreSQL.** The audit trail is the most sensitive data this system holds — every agent decision has to be reconstructable later, exactly as it happened. That requires real transactions (a decision and its audit entry commit together or not at all), foreign key constraints (an audit entry can't silently point at a decision that doesn't exist), and enforced schema (a malformed record should be rejected at write time, not discovered later during an incident review). MongoDB can enforce this too, but only when validation is explicitly configured per collection; Postgres enforces it by default, which is the safer starting point for something as safety-critical as an audit log. Postgres gives us all of that, plus the option to add an append-only, hash-chained audit table with real constraints enforcing the chain — something worth designing around a real relational engine, not a file-based one.

**Redis.** Nothing in Phase 0 reads or writes Redis yet — this is the one honest bet in this ADR. It's included now because of what's coming next: in Phase 2 the agent pipeline moves to a background worker, and workers need a job queue between the API and the worker process. Redis is also the natural fit for the STM (short-term memory)/session state layer once agents exist, and later for caching and rate-limiting the governance gate. Wiring up Redis now — one more container, one more healthcheck — costs almost nothing, whereas retrofitting a queue and its connection handling into an already-running API later is a real migration. If the agent architecture changes enough that Redis stops making sense, that's a decision for a future ADR to supersede this one.

### 2. Monorepo with `apps/`

One repo holds `apps/api` and `apps/web` now, with `apps/worker` expected once the agent pipeline needs its own process. A single repo means one PR can change the API's response shape and the frontend's handling of it together, reviewed and merged as one unit — with separate repos, that change is two PRs across two repos that have to be coordinated and merged in the right order, with a window where they're out of sync. It also means one Compose file, one set of environment conventions, and one place to look for "how does the whole system fit together," which matters more here than in a typical company setup because this is a portfolio project meant to be read end-to-end by someone evaluating it.

### 3. `API_URL` (server-side) vs `NEXT_PUBLIC_API_URL` (browser-side)

These resolve differently depending on where the code runs. `API_URL` is read only by code executing on the Next.js server (Server Components, route handlers) and inside Docker resolves to `http://api:8000` — the Compose service name, reachable only from other containers on the same Docker network. `NEXT_PUBLIC_API_URL` is inlined into the JavaScript bundle at build time and would be read by code running in the user's browser, which is outside the Docker network entirely and would need a real, publicly reachable host and port — `http://localhost:8000` in local dev, a real domain in any deployed environment.

One variable can't serve both because the two are answering different questions from different machines. Set one shared variable to `http://localhost:8000` and the Next.js container can't reach the API, because `localhost` inside a container refers to the container itself, not the host or its neighbors. Set it to `http://api:8000` instead and the browser can't resolve it, because `api` only exists as a hostname on the Docker network — a browser on the person's own machine has no route to it. Since Phase 0 has no browser-side fetches at all — the health check is entirely server-fetched — `NEXT_PUBLIC_API_URL` will be declared in `.env.local.example` as reserved and unused rather than wired to anything yet (P0-5).

## Consequences

### Positive
- The agent pipeline (Phase 1+) can be built directly in the same language and process model as the rest of the backend, with no service boundary to design around prematurely.
- The browser never needs direct network access to the API in Phase 0, which sidesteps CORS configuration and reduces the API's exposed surface area.
- Postgres gives the audit log real transactional guarantees from the start, rather than retrofitting them onto a schema-less store later.
- Adding a worker process later (Phase 2) is additive — Redis and Compose conventions are already in place, not something to introduce alongside the first agent.
- One repo, one Compose file, one README: someone evaluating this project can find everything without switching repos.

### Negative / trade-offs
- Redis runs in every environment from Phase 0 onward despite doing nothing yet — one more container, one more healthcheck, one more thing that can fail, in service of a bet on future architecture rather than a current need.
- The monorepo makes CI more complex than it would be for a single small app: a push touching only frontend code still triggers backend lint/test jobs (and vice versa) unless CI is later split with path filters, which Phase 0 does not do.
- A monorepo couples release cadence — the API and frontend version and deploy together by default, which is convenient now but would need deliberate unwinding (separate deploy pipelines, versioning) if the two ever needed to scale or ship independently.
- Async Python introduces its own class of mistake that synchronous code doesn't have: a single blocking (synchronous) call inside an async route handler — a non-async DB driver, a synchronous `requests.get`, a CPU-bound loop — silently stalls the entire event loop, freezing every other in-flight request on that worker rather than just the one that made the call. This is a real cost incurred specifically to support the concurrent agent workloads coming in later phases, and it means async-safe libraries have to be chosen deliberately throughout the codebase, not assumed.

## Alternatives considered

| Decision | Alternative | Why not chosen |
|---|---|---|
| Backend | Node/Express or NestJS | Would unify the language with the frontend, but the agent/LLM tooling ecosystem (LangGraph, LangChain, model SDKs) is overwhelmingly Python-first; building the core pipeline in Node would mean less mature tooling or a second Python service anyway. |
| Backend | Django | Batteries-included and has a strong admin/ORM story, but its synchronous-by-default design and heavier project structure work against a service that's mostly async I/O to LLMs and datastores; FastAPI is lighter and async-native from the start. |
| Backend | Flask | Flask 2.0+ supports `async` views, but it remains a WSGI framework, so async is bolted onto a synchronous request model rather than native — each request still occupies a worker, so you get async syntax without async concurrency. Request validation also needs extra libraries. FastAPI is ASGI and async-native, with Pydantic validation built in. |
| Frontend | Vite + React SPA | Simpler to set up, but has no server tier: the browser would have to call the API directly, requiring CORS configuration and a publicly reachable API from day one — exactly what Decision 3 avoids. |
| Database | SQLite | Fine for Phase 0's actual usage, but doesn't match where this is going: an audit log intended to run under concurrent agent writes and be queried by a dashboard needs a real client-server database, not a single-file one with limited concurrent-write support. |
| Database | MongoDB | MongoDB does offer schema validation and multi-document transactions, but both are opt-in and configured per collection rather than enforced by default. An audit log should fail safe: Postgres makes constraints and transactions the default behavior, so a developer has to deliberately loosen them to let a malformed record through, rather than deliberately add validation to catch one. |
| Repo layout | Separate repos per app | Cleaner ownership boundaries if this were a multi-team project, but for a single-person portfolio project it means coordinating PRs across repos for any change that touches both the API contract and its consumer — pure overhead here. |
| API URL config | One shared environment variable | Works only when frontend and backend are both reachable from the same network context; breaks the moment the frontend is containerized and the API is not meant to be reachable from the browser, which is the case here. |
