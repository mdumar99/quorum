import re

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://unused", redis_url="redis://unused", log_level="LOUD")


def test_settings_has_no_owner_or_superuser_url(monkeypatch):
    """The API holds the app-role URL only (ADR-0002, Decision 1); the owner URL stays out."""
    # No field is named for the owner or the superuser...
    for name in Settings.model_fields:
        assert not re.search(r"owner|superuser|postgres", name), name
    # ...and even with those values in the environment, Settings does not pick them up.
    monkeypatch.setenv("OWNER_DATABASE_URL", "postgresql://quorum_owner:unused@host/db")
    monkeypatch.setenv("POSTGRES_USER", "unused-superuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "unused-superuser-password")
    settings = Settings(database_url="postgresql://unused", redis_url="redis://unused")
    dumped = str(settings.model_dump())
    assert "quorum_owner" not in dumped
    assert "unused-superuser" not in dumped
    assert not any("owner" in name for name in vars(settings))
