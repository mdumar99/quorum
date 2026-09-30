# Contributing to Quorum

## First-time setup
```bash
cp .env.example .env
cp apps/web/.env.local.example apps/web/.env.local

uv tool install pre-commit
pre-commit install

# native dev only — skip if you're only running via Docker
(cd apps/api && uv sync)
(cd apps/web && npm ci)
```

## Python: always `uv run`
Never call `python`, `pip`, or `pytest` directly — always `uv run <command>`. A bare `python` picks up whatever interpreter happens to be first on your `PATH`, which is a different one depending on the machine, the shell, and what else you've installed; `uv run` guarantees you're using the exact interpreter and locked dependency versions this project expects, every time. Add dependencies with `uv add` (or `uv add --dev` for dev-only tools) rather than editing `pyproject.toml` by hand, and always commit the resulting `uv.lock` change in the same commit — an unlocked dependency bump is a bug waiting for someone else's machine to hit.

## Node
Use `npm ci`, not `npm install`, for a clean install — it installs exactly what's in `package-lock.json` and fails loudly if the lockfile and `package.json` disagree, instead of silently updating things. Prettier is pinned to an exact version in both `apps/web/package.json` and the local pre-commit hook; if you bump one, bump the other in the same commit, or pre-commit and CI will start disagreeing about formatting.

## Line endings
LF everywhere, enforced by `.gitattributes` — mixed line endings are how a script that runs fine on your machine fails with a `bad interpreter` error inside a Linux container. If you're on Windows or WSL, run once:
```bash
git config --global core.autocrlf input
```

## Formatting and linting
Pre-commit runs on every commit: standard hygiene hooks, `ruff check` and `ruff format` for Python, and a local Prettier hook for the web app (`apps/web`). ESLint isn't in pre-commit — it needs the full `apps/web` install to run correctly, so it's enforced in CI instead. Don't commit with `--no-verify` to skip pre-commit: CI re-runs the same checks on every pull request, so skipping locally will only delay the failure, not avoid it.

## Branches
`<type>/p0-N-short-description`, e.g. `feat/p0-3-health-endpoint`. Types in use: `feat`, `fix`, `chore`, `docs`, `test`, `style`.

## Commits
[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <summary> (#N)`. The ticket number keeps every commit traceable back to the issue that explains why it exists, without having to dig through PR history later.

## Pull requests
One ticket, one branch, one PR — squash-merge only, so `main` gets one clean commit per ticket instead of a tangle of intermediate "fix typo" commits. Use the PR template. Every PR body ends with `Closes #N`; check that GitHub actually shows the issue as linked before merging, not just that you typed the line.

## Architecture decisions
Decisions that are expensive to reverse — the stack, the repo layout, a networking split between services — go in [`docs/adr/`](docs/adr/README.md), not here. This file is for the how; ADRs are for the why behind things that would be costly to undo.
