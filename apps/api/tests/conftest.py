import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app


@pytest.fixture
def client():
    """Integration client: real config, real lifespan, real Postgres/Redis."""
    with TestClient(app) as c:  # `with` runs the lifespan (startup/shutdown)
        yield c


@pytest.fixture
def unit_client():
    """Unit client: fake config, no lifespan. Works with no .env and no containers."""
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql://unused", redis_url="redis://unused"
    )
    yield TestClient(app)  # no `with`: lifespan (and its real config load) is skipped
    app.dependency_overrides.clear()  # don't leak the override into other tests


@pytest.fixture
def owner_database_url() -> str:
    """The owner role's URL, for tests that need the owner (ADR-0002, Decision 1).

    Read from the environment, never from Settings: the owner URL must not become an API
    setting. A missing value is a failure, not a skip, so a misconfigured CI cannot pass
    by silently not testing the roles.
    """
    url = os.environ.get("OWNER_DATABASE_URL")
    if not url:
        pytest.fail(
            "OWNER_DATABASE_URL is not set. Export it for tests (it is in .env.example), "
            "for example: set -a; . ./.env; set +a"
        )
    return url
