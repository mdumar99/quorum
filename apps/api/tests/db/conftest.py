import pytest
from db_helpers import CLOSED_PORT_URL

from app.config import Settings, get_settings


@pytest.fixture
def no_pool_env(monkeypatch):
    """Nothing from the process environment may decide a Settings test."""
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "LOG_LEVEL",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS",
        "FAKE_LLM_MODE",
        "DB_POOL_MIN_SIZE",
        "DB_POOL_MAX_SIZE",
        "DB_POOL_CHECKOUT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def boot_env(monkeypatch, no_pool_env):
    """The lifespan with a closed-port database URL: no database, no DNS, refused at once.

    A developer's root .env is turned off with monkeypatch.setitem (restored afterwards), and the
    settings cache is cleared before and after, so nothing leaks into other tests.
    """
    monkeypatch.setenv("DATABASE_URL", CLOSED_PORT_URL)
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
