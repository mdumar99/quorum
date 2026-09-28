import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request

from app.config import get_settings
from app.logging_config import configure_logging

configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast: load config at startup so a missing variable crashes the boot,
    # not the first request an hour later.
    get_settings()
    log.info("startup_complete")
    yield
    log.info("shutdown")


app = FastAPI(title="Quorum API", version="0.1.0", lifespan=lifespan)


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
