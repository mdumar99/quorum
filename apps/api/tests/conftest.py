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
