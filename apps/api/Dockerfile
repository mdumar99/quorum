# syntax=docker/dockerfile:1

# ---------- Stage 1: install dependencies with uv ----------
FROM python:3.13-slim AS builder

# Copy the uv binary from its official image (pinned to the version used locally).
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/

# Compile .pyc at build time (faster startup); copy files instead of hard-linking;
# use the image's Python rather than downloading another one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies FIRST, code SECOND. Docker caches each layer, so this expensive
# step only re-runs when pyproject.toml or uv.lock change, not on every code edit.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# ---------- Stage 2: slim runtime image ----------
FROM python:3.13-slim

# Never run the app as root inside the container.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser
EXPOSE 8000

# Healthcheck: python:3.13-slim has no curl, so docker-compose.yml probes /health
# with a Python one-liner instead of installing extra tools.
# --host 0.0.0.0: listen on all interfaces INSIDE the container so Docker can
# forward traffic in. (Host-side exposure is still limited by 127.0.0.1 in Compose.)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]