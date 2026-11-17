"""docs/llm.md covers the settings, the fake's modes, the error mapping and the NUL rule (#48).

Reads the doc as text: no database and no app code. Every name the doc must mention comes from
the code, so a new mode or error cannot be added without the doc.
"""

import re

import pytest
from llm_helpers import PROVIDER_ERROR_TYPES, REPO_ROOT

from app.llm import FAKE_LLM_MODES

DOC = REPO_ROOT / "docs" / "llm.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.is_file(), "docs/llm.md does not exist yet"
    return DOC.read_text()


@pytest.mark.parametrize(
    "setting", ["LLM_PROVIDER", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS", "FAKE_LLM_MODE"]
)
def test_every_setting_is_named(text, setting):
    assert setting in text


@pytest.mark.parametrize("name", [*FAKE_LLM_MODES, *PROVIDER_ERROR_TYPES])
def test_every_fake_mode_and_every_error_is_named(text, name):
    assert f"`{name}`" in text


def test_the_capture_rules_are_stated(text):
    assert "U+001A" in text  # decision 3: NUL
    assert "U+FFFD" in text  # lone surrogates
    assert "16,384" in text
    assert "SHA-256" in text


def test_the_groq_stop_is_stated(text):
    assert "P1-14" in text


def test_every_path_the_doc_names_exists(text):
    for path in sorted(set(re.findall(r"`((?:apps|docs)/[A-Za-z0-9_./-]+\.(?:py|md))`", text))):
        assert (REPO_ROOT / path).is_file(), path
