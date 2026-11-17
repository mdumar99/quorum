import pytest


@pytest.fixture
def no_llm_env(monkeypatch):
    """Nothing from the process environment may decide a Settings test."""
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "LOG_LEVEL",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS",
        "FAKE_LLM_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
