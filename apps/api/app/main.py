import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request

from app.config import get_settings
from app.db.pool import POOL_CLOSE_TIMEOUT_S, POOL_STARTED_EVENT, create_pool
from app.llm import build_llm_client
from app.logging_config import configure_logging
from app.policy import load_current_policy
from app.routers import health

configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast: load config at startup so a missing variable crashes the boot,
    # not the first request an hour later.
    settings = get_settings()  # fail fast on missing/invalid config
    configure_logging(settings.log_level)
    # Validate the policy file once, at startup: an invalid file raises here and stops the boot.
    app.state.policy = load_current_policy()
    log.info("policy_loaded", policy_version=app.state.policy.policy_version)
    # Built once and kept on app.state, like the policy. `groq` raises here and stops the boot
    # until P1-14. The log names the provider and the model, never the key.
    app.state.llm_client = build_llm_client(settings)
    log.info(
        "llm_client_ready",
        provider=app.state.llm_client.provider,
        model_id=app.state.llm_client.model_id,
    )
    # The pool is created LAST, so nothing after it can fail and leave its workers running. A URL
    # that is not a connection string raises here (before anything is started) and stops the boot;
    # an unreachable database never does: `open(wait=False)` returns at once and the workers keep
    # trying in the background. The log names the sizes and the timeout, never the URL.
    pool = create_pool(settings)
    try:
        await pool.open(wait=False)
        app.state.pool = pool
        log.info(
            POOL_STARTED_EVENT,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            checkout_timeout_seconds=settings.db_pool_checkout_timeout_seconds,
        )
        log.info("startup_complete", log_level=settings.log_level)
        yield
    finally:
        # Bounded: with the database down this cannot hang a stop (it waits at most the timeout).
        await pool.close(timeout=POOL_CLOSE_TIMEOUT_S)
    log.info("shutdown")


app = FastAPI(title="Quorum API", version="0.1.0", lifespan=lifespan)

app.include_router(health.router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # One JSON line per request. Replaces uvicorn's plain-text access log (see --no-access-log).
    start = time.perf_counter()
    response = await call_next(request)
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - start) * 1000, 1),
    )
    return response
