"""docs/policy.md says how to add a version and shows the real JSON shape (#46: AC11).

It reads the doc as text: no database and no app code are needed. The two JSON examples in the
doc must equal the golden examples that the shape test checks the code against, and every path
the doc names must exist, all written in full from the repo root.
"""

import json
import re
from pathlib import Path

import pytest
from policy_examples import EXAMPLE_ALL_PASS, EXAMPLE_R1_R2_FAIL

# tests/policy/test_policy_doc.py -> policy -> tests -> api -> apps -> repo root
ROOT = Path(__file__).resolve().parents[4]
DOC = ROOT / "docs" / "policy.md"
TEMPLATE_PATH = "apps/api/app/policy/versions/v<N>.toml"
PATH_SUFFIXES = (".toml", ".py", ".md")
PATH_PREFIXES = ("apps/", "docs/")


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.is_file(), "docs/policy.md does not exist yet"
    return DOC.read_text(encoding="utf-8")


def named_paths(text: str) -> list[str]:
    """The backtick tokens that look like paths. Tokens with "<" are templates and are skipped."""
    tokens = re.findall(r"`([^`\n]+)`", text)
    looks_like_a_path = [
        token
        for token in tokens
        if " " not in token and (token.endswith(PATH_SUFFIXES) or token.startswith(PATH_PREFIXES))
    ]
    return [token for token in looks_like_a_path if "<" not in token]


def test_doc_json_examples_equal_the_golden_examples(text):
    blocks = re.findall(r"^```json\n(.*?)^```", text, flags=re.DOTALL | re.MULTILINE)
    assert len(blocks) >= 2, "the doc needs two json blocks: the all-pass and the R1+R2 example"
    assert json.loads(blocks[0]) == EXAMPLE_ALL_PASS
    assert json.loads(blocks[1]) == EXAMPLE_R1_R2_FAIL


def test_every_path_the_doc_names_exists(text):
    paths = named_paths(text)
    assert "apps/api/app/policy/versions/v1.toml" in paths
    assert "docs/policy.md" in paths
    for path in paths:
        assert path.startswith(PATH_PREFIXES), f"{path} is not written in full from the repo root"
        assert (ROOT / path).exists(), f"{path} does not exist"


def test_doc_names_the_next_version_file_with_a_template_path(text):
    # The template contains "<", so the path check above skips it; this checks it is written.
    assert f"`{TEMPLATE_PATH}`" in text


def test_doc_says_a_released_version_is_never_edited(text):
    assert "never edited" in text
