import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal

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

CheckFactory = Callable[[], Coroutine[Any, Any, None]]

# One in-flight check per dependency. If a check outlives its request's timeout,
# later requests wait on THAT task instead of starting another. Without this, every
# request during an outage started a new DNS lookup that held a thread for ~8s,
# which exhausted the event loop's small resolver pool and made healthy
# dependencies time out too (#15).
_in_flight: dict[str, asyncio.Task[None]] = {}


def _consume_result(task: asyncio.Task[None]) -> None:
    """Mark a background check's outcome as retrieved (avoids 'exception never retrieved')."""
    if not task.cancelled():
        task.exception()


class HealthResponse(BaseModel):
    """The /health contract. P0-4 (frontend) and P0-9 (smoke test) depend on this shape."""

    status: Status
    db: Status
    redis: Status


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def _run_check(name: str, make_check: CheckFactory) -> Status:
    """Run one dependency check with a hard time limit, sharing any check already in flight."""
    start = time.perf_counter()
    task = _in_flight.get(name)
    if task is None or task.done():
        task = asyncio.create_task(make_check())
        task.add_done_callback(_consume_result)
        _in_flight[name] = task
    else:
        log.debug("health_check_joined_in_flight", dependency=name)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            # shield: the timeout cancels our WAIT, not the shared check itself.
            await asyncio.shield(task)
        log.debug("health_check_ok", dependency=name, duration_ms=_elapsed_ms(start))
        return "ok"
    except Exception as exc:
        # Log the reason for operators; never put it in the response (it can leak internals).
        log.warning(
            "health_check_failed",
            dependency=name,
            error=type(exc).__name__,
            duration_ms=_elapsed_ms(start),
        )
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
        _run_check("db", lambda: ping_postgres(settings.database_url, CHECK_TIMEOUT_S)),
        _run_check("redis", lambda: ping_redis(settings.redis_url, CHECK_TIMEOUT_S)),
    )
    healthy = db == "ok" and redis == "ok"
    if not healthy:
        response.status_code = 503
    return HealthResponse(status="ok" if healthy else "error", db=db, redis=redis)
