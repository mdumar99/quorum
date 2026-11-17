"""The client is built once in the lifespan, on app.state; a bad config stops the boot (#48).

Unit tests: no database. The settings cache is cleared before and after, a developer's root .env
is turned off with monkeypatch.setitem (restored afterwards), and logs are read from stdout with
capsys: structlog caches its logger on first use, which makes `capture_logs` see nothing.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import ROOT_ENV_FILE, Settings, get_settings
from app.llm import (
    FakeLLMClient,
    LLMConfigError,
    LLMRequest,
    RateLimitedError,
    build_llm_client,
)
from app.main import app

KEY = "KEYMARK12345"


@pytest.fixture
def boot_env(monkeypatch, no_llm_env):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # restored by monkeypatch
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def boot():
    with TestClient(app):
        pass


def test_boot_without_llm_provider_fails(boot_env):
    with pytest.raises(ValidationError) as excinfo:
        boot()
    assert "llm_provider" in str(excinfo.value)


def test_boot_with_fake_builds_one_client_on_app_state(boot_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    with TestClient(app):
        client = app.state.llm_client
        assert isinstance(client, FakeLLMClient)
        assert client.model_id == "fake-v1"


def test_the_client_gets_the_configured_timeout(boot_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "3.5")
    with TestClient(app):
        client = app.state.llm_client
        assert isinstance(client, FakeLLMClient)
        assert client.timeout_seconds == 3.5


def test_boot_with_fake_mode_from_the_environment_builds_that_mode(boot_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("FAKE_LLM_MODE", "rate_limited")
    with TestClient(app):
        client = app.state.llm_client
        assert isinstance(client, FakeLLMClient)
        with pytest.raises(RateLimitedError):
            asyncio.run(client.complete(LLMRequest(prompt="p", prompt_version="pv-1")))


def test_the_startup_log_names_the_provider_and_never_the_key(boot_env, monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_API_KEY", KEY)
    boot()
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    ready = [e for e in events if e["event"] == "llm_client_ready"]
    assert len(ready) == 1
    assert ready[0]["provider"] == "fake"
    assert ready[0]["model_id"] == "fake-v1"
    assert KEY not in out


def test_boot_with_groq_and_a_key_stops_with_a_clear_message(boot_env, monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", KEY)
    with pytest.raises(LLMConfigError) as excinfo:
        boot()
    message = str(excinfo.value)
    assert "groq" in message.lower()
    assert "P1-14" in message
    assert KEY not in message
    assert KEY not in capsys.readouterr().out


def test_boot_with_groq_and_no_key_fails_settings_validation_naming_llm_api_key(
    boot_env, monkeypatch
):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    with pytest.raises(ValidationError) as excinfo:
        boot()
    assert "llm_api_key" in str(excinfo.value)


# ---------- the factory ----------


def make_settings(**kwargs) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://unused",
        redis_url="redis://unused",
        **kwargs,
    )


def test_the_factory_builds_the_fake_with_the_mode_and_the_timeout(no_llm_env):
    client = build_llm_client(
        make_settings(
            llm_provider="fake", fake_llm_mode="provider_unavailable", llm_timeout_seconds=2
        )
    )
    assert isinstance(client, FakeLLMClient)
    assert client.mode == "provider_unavailable"
    assert client.timeout_seconds == 2


def test_the_fake_mode_has_no_effect_when_the_provider_is_not_fake(no_llm_env):
    """FAKE_LLM_MODE is read only for `fake`: with groq the result is the groq stop, not a fake."""
    settings = make_settings(llm_provider="groq", llm_api_key=KEY, fake_llm_mode="llm_timeout")
    with pytest.raises(LLMConfigError, match="P1-14"):
        build_llm_client(settings)


def test_the_env_file_setting_is_back_to_the_root_env_after_the_boot_tests():
    """The boot fixture turns the root .env off with setitem; none of that may leak out."""
    assert Settings.model_config.get("env_file") == ROOT_ENV_FILE
