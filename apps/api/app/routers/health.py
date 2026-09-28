import asyncio
from collections.abc import Awaitable
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db.redis_client import ping_redis
from app.db.session import ping_postgres

router = APIRouter()
log = structlog.get_logger()

CHECK_TIMEOUT_S = 2.0
Status = Literal["ok", "error"]


class HealthResponse(BaseModel):
    """The /health contract. P0-4 (frontend) and P0-9 (smoke test) depend on this shape."""

    status: Status
    db: Status
    redis: Status


async def _run_check(name: str, check: Awaitable[None]) -> Status:
    """Run one dependency check with a hard time limit. Never raises."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await check
        return "ok"
    except Exception as exc:
        # Log the reason for operators; never put it in the response (it can leak internals).
        log.warning("health_check_failed", dependency=name, error=type(exc).__name__)
        return "error"


@router.get(
    "/health",
    responses={503: {"model": HealthResponse, "description": "A dependency is unreachable"}},
)
async def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    # Both checks run concurrently: worst case is ~2s total, not 2s + 2s.
    db, redis = await asyncio.gather(
        _run_check("db", ping_postgres(settings.database_url, CHECK_TIMEOUT_S)),
        _run_check("redis", ping_redis(settings.redis_url, CHECK_TIMEOUT_S)),
    )
    healthy = db == "ok" and redis == "ok"
    if not healthy:
        response.status_code = 503
    return HealthResponse(status="ok" if healthy else "error", db=db, redis=redis)