import asyncio
import time

import pytest

from app.config import get_settings
from app.db.redis_client import ping_redis
from app.db.session import ping_postgres
from app.routers import health


async def _ok(*args, **kwargs) -> None:
    return None


async def _fail(*args, **kwargs) -> None:
    raise ConnectionRefusedError("simulated outage")


async def _hang(*args, **kwargs) -> None:
    await asyncio.sleep(30)


# ---------- Integration: real Postgres + Redis (docker compose up -d --wait) ----------


@pytest.mark.integration
def test_health_returns_200_when_dependencies_up(client):
    assert client.get("/health").status_code == 200


@pytest.mark.integration
def test_health_body_matches_contract_when_healthy(client):
    assert client.get("/health").json() == {"status": "ok", "db": "ok", "redis": "ok"}


@pytest.mark.integration
def test_ping_postgres_succeeds_against_real_postgres():
    asyncio.run(ping_postgres(get_settings().database_url, timeout_s=2))  # raises on failure


@pytest.mark.integration
def test_ping_redis_succeeds_against_real_redis():
    asyncio.run(ping_redis(get_settings().redis_url, timeout_s=2))  # raises on failure


# ---------- Unit: dependencies mocked, no containers needed ----------


def test_returns_503_when_db_unreachable(unit_client, monkeypatch):
    monkeypatch.setattr(health, "ping_postgres", _fail)
    monkeypatch.setattr(health, "ping_redis", _ok)
    response = unit_client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "redis": "ok"}


def test_returns_503_when_redis_unreachable(unit_client, monkeypatch):
    monkeypatch.setattr(health, "ping_postgres", _ok)
    monkeypatch.setattr(health, "ping_redis", _fail)
    response = unit_client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "ok", "redis": "error"}


def test_hanging_dependency_is_cut_off_by_timeout(unit_client, monkeypatch):
    """Beyond the ticket's 6 cases: automates the manual `docker compose pause` test."""
    monkeypatch.setattr(health, "CHECK_TIMEOUT_S", 0.1)
    monkeypatch.setattr(health, "ping_postgres", _hang)
    monkeypatch.setattr(health, "ping_redis", _ok)
    start = time.perf_counter()
    response = unit_client.get("/health")
    assert response.status_code == 503
    assert time.perf_counter() - start < 1.0  # would be ~30s without the timeout
