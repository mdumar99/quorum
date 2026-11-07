# Quorum: instructions for Claude Code

Quorum is a governed multi-agent AI system for e-commerce returns: FastAPI (`apps/api`), Next.js (`apps/web`), PostgreSQL and Redis, all under Docker Compose. Phase 0 (v0.1.0) shipped the skeleton. Phase 1 adds the data model and a first single-agent decision pipeline.

## How we work (most important section)

I'm using this project to learn. You do the work, but I must understand and approve each step.

- **Explain before acting.** Before each state-changing step, say in 1–3 sentences what you'll do and why. Tables are welcome for "what each piece does".
- **One state-changing step at a time.** Never chain `git commit` with `git push` or `gh pr …`. Wait for the result before the next step.
- **Stop at decisions.** Anything that is a design choice (an ADR decision, a schema choice, a trade-off) gets presented as options with your lean. I decide. Don't decide silently. The only exception is the small reversible choices in [Decision rule](#decision-rule) below, which are decided and logged there.
- **Evidence before claims.** Verify facts (versions, library behaviour, CLI flags) by running something or reading docs before relying on them. Say when something is unverified.
- **Point out mistakes directly**, including your own.
- **End each completed step with a retro line:** `📝 Retro (<section>): <lesson>`, and append it to `~/code/quorum-notes/phase1-retro-notes.md` under that section. Sections: Shell & tooling / Git / Line endings & formatting / GitHub & project management / Markdown / Writing (ADRs & PRs) / Docker & networking / Backend & testing / LLM & agents.

### Decision rule

Opus may decide and log small reversible choices. Everything else stops for me. This is the only exception to "Stop at decisions".

**Opus decides and logs:**
- naming
- layout inside already-decided ADRs
- test structure
- ticket order
- drafts of docs and PR text

**Stops for me:**
- ADR content
- schema, migration and role changes
- GitHub settings and secrets
- adding or removing a dependency or service
- public text like the README and the PR `## Why`
- anything irreversible or costing money
- edits to `CLAUDE.md` or `.claude/settings.json`

Anything outside both lists, or unclear which list it belongs to, stops for me.

**Who decides and how it is logged.** Opus decides only in plan mode (`opusplan`). Input from a mid-task advisor call is advice: Sonnet decides and is recorded as the decider. Entries go in `~/code/quorum-notes/decisions.md` with six fields: id, date, decision, decided by, why, status. "Decided by" is exactly one of:
- `user`
- `Opus (decided in plan mode under opusplan)`
- `Sonnet, on Opus advice (advisor consulted)`

If Opus is not in the loop (no plan mode), Sonnet consults the advisor before deciding any item from the "Opus decides" list that passes the logging threshold, and is recorded as the decider; if the advisor is unavailable, it stops for me.

**What is worth logging.** Log a decision, whoever made it, only if someone might want to revisit it:
- a public-facing name
- a choice affecting more than one file
- a choice made over a considered alternative

Trivial local choices (a local variable name, the order of things inside one function) are not logged.

**What this can't enforce.** This is a convention, not enforcement. Sonnet decides when to call the advisor, and the log is only as complete as the model's discipline. Only `settings.json` rules are hard.

## Phase lifecycle (same as Phase 0)

1. Plan review → 2. decisions and ADRs → 3. ticket drafts reviewed, then created → 4. per-ticket loop → 5. phase close (retro, release tag).

### Tickets
- Drafted in `~/code/quorum-issue-drafts/` first, shown to me, then created with `gh issue create --body-file …`.
- Body sections: `## Context`, `## Proposed approach`, `## Acceptance criteria` (checkboxes).
- Labels: one `type:*` label (`type:feature`, `type:chore`, `type:infra`, `type:bug`, `type:docs`), plus `tech-debt` where it applies. Milestone: `Phase 1`.
- Board: GitHub Project #2 (owner `mdumar99`). Auto-add should put new issues in Todo. Check with `gh project item-list 2 --owner mdumar99`, and only if the issue is missing run `gh project item-add 2 --owner mdumar99 --url <issue-url>`.
- Issues and PRs share one number counter.

### Per-ticket loop
1. `git switch main && git pull`, then a branch: `<type>/p<phase>-<issue#>-<short-name>` (the exact format is in the Branches section of CONTRIBUTING.md).
2. Implement in small steps, running the relevant tests locally.
3. Show me `git diff --stat` and the key parts of the diff before committing.
4. Conventional commit message (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `ci:`).
5. Push, then `gh pr create` with sections: `## What`, `## Why`, `## How tested`, `## Alternatives considered`, `## Known limitations`, and `Closes #<n>`. I write or approve the Why and the verdicts.
6. `gh pr checks <n> --watch`. All 5 required checks must pass.
7. Before merging, show me the decisions made during this ticket: the new entries in `~/code/quorum-notes/decisions.md`, or say there are none. I review them before the merge.
8. `gh pr merge <n> --squash --delete-branch`, then sync main.

## Hard rules
- Never push to `main`, never use `--no-verify`, never force-push a shared branch, never bypass the ruleset.
- Never commit secrets or a real `.env`. Only `*.example` files hold placeholder values.
- ADRs live in `docs/adr/`. An ADR is Accepted when its PR merges and is never edited afterwards: changes need a new ADR that supersedes it.
- Spikes run on `spike/*` branches that are never merged.

## Commands
- Full stack: `docker compose up -d --build --wait` (stop it with `docker compose down`; `-v` wipes data: never run; I'll run it myself)
- Datastores only: `docker compose up -d --wait postgres redis`
- API tests: `cd apps/api && uv run pytest` (`-m "not integration"` skips tests that need Postgres/Redis)
- API lint/format: `cd apps/api && uv run ruff check . && uv run ruff format --check .`
- Web: `cd apps/web && npm test`, `npm run lint`, `npm run build`, `npx tsc --noEmit`
- Smoke test: `docker compose down && bash scripts/smoke-test.sh`
- All hooks: `pre-commit run --all-files`

## Environment notes
- WSL2 Ubuntu with Docker Desktop. Postgres is published on host port 5433 locally (`POSTGRES_HOST_PORT`), because a Windows Postgres holds 5432.
- Python via `uv` (3.13), Node 24 via `nvm`.
