"""The policy is loaded and validated at startup; an invalid file fails boot (#46: AC2).

Unit tests: the lifespan reads the settings and the policy and touches no database, so dummy
settings are enough. `VERSIONS_DIR` is replaced with monkeypatch; these tests also fail if the
loaders bound their `directory` default at import time instead of reading it at call time.
"""

import pytest
from fastapi.testclient import TestClient
from policy_examples import (
    V1_POLICY_VERSION,
    V1_TEXT,
    canonical_hash,
    doc_from,
    toml_from,
    write_version,
)

from app.config import get_settings
from app.main import app
from app.policy import PolicyError, loader


@pytest.fixture
def boot_env(monkeypatch):
    """Dummy settings for the lifespan; the settings cache is cleared before and after."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_valid_policy_is_loaded_at_startup(boot_env):
    with TestClient(app):
        assert app.state.policy.policy_version == V1_POLICY_VERSION


BAD_FILES = {
    "missing-key": V1_TEXT.replace("window_days = 30\n", ""),
    "unknown-key": V1_TEXT.replace("window_days = 30\n", "window_days = 30\ngrace = 2\n"),
    "float-ceiling": V1_TEXT.replace("ceiling_minor = 20000\n", "ceiling_minor = 20000.0\n"),
    "wrong-type": V1_TEXT.replace("window_days = 30\n", 'window_days = "30"\n'),
}


@pytest.mark.parametrize("text", BAD_FILES.values(), ids=BAD_FILES.keys())
def test_invalid_policy_file_fails_boot(boot_env, monkeypatch, tmp_path, text):
    write_version(tmp_path, "v1", text)
    monkeypatch.setattr(loader, "VERSIONS_DIR", tmp_path)
    with pytest.raises(PolicyError):
        with TestClient(app):
            pass


def test_boot_uses_the_directory_resolved_at_call_time(boot_env, monkeypatch, tmp_path):
    write_version(tmp_path, "v1", toml_from(ceiling=30000))
    monkeypatch.setattr(loader, "VERSIONS_DIR", tmp_path)
    with TestClient(app):
        expected = f"v1:{canonical_hash(doc_from(ceiling=30000))}"
        assert app.state.policy.policy_version == expected
        assert app.state.policy.rules.R3.ceiling_minor == 30000
