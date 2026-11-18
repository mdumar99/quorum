"""docs/data-access.md covers the pool settings, the outage behaviour and the numbers (issue #50).

It reads the doc as text: no database and no app code run. Every name the doc must mention comes
from the code, and the default it states for each setting must equal the default in Settings, so the
doc cannot drift from the code. The settings table has the columns Setting, Default, Meaning.
"""

import re

import pytest
from db_helpers import REPO_ROOT

from app.config import Settings
from app.db.pool import POOL_STARTED_EVENT

DOC = REPO_ROOT / "docs" / "data-access.md"
POOL_SETTINGS = sorted(name for name in Settings.model_fields if name.startswith("db_pool_"))


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.is_file(), "docs/data-access.md does not exist yet"
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", POOL_SETTINGS)
def test_every_pool_setting_is_named(text, field):
    assert f"`{field.upper()}`" in text


@pytest.mark.parametrize("field", POOL_SETTINGS)
def test_the_default_the_doc_states_is_the_default_in_settings(text, field):
    rows = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\| `(DB_POOL_[A-Z_]+)` \| ([^|]+?) \|.*", line)
        if match:
            rows[match.group(1)] = match.group(2)
    assert field.upper() in rows, f"{field.upper()} has no row in the settings table"
    assert float(rows[field.upper()]) == float(Settings.model_fields[field].default)


def test_the_log_event_is_named(text):
    assert f"`{POOL_STARTED_EVENT}`" in text


def test_the_doc_says_that_compose_passes_none_of_the_settings(text):
    lines = [line for line in text.splitlines() if "docker-compose.yml" in line]
    assert any("none of" in line.lower() for line in lines)


def test_every_path_the_doc_names_exists(text):
    found = re.findall(r"`((?:apps|docs)/[A-Za-z0-9_./-]+\.(?:py|md|toml))`", text)
    paths = sorted(set(found))
    assert paths, "the doc names no path (a test over an empty list proves nothing)"
    for path in paths:
        assert (REPO_ROOT / path).is_file(), path
