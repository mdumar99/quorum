"""The env example, Compose and CI carry the LLM settings (#48: the CI env change in the same PR).

Text checks on the committed files: nothing runs Docker. A clean clone, `cp .env.example .env`,
must boot, so the example is also loaded through Settings.
"""

import re

import pytest
from llm_helpers import REPO_ROOT

from app.config import Settings

ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "docker-compose.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def read(path) -> str:
    assert path.is_file(), f"{path.name} not found"
    return path.read_text()


def job_section(text: str, name: str) -> str:
    """The text under the key `  <name>:` (two-space indent), up to the next key at that indent.

    Used for a Compose service and a CI job. It stops at the next `  <other>:` line, so the text
    of one job can never include a later one.
    """
    start = re.search(rf"^  {re.escape(name)}:\s*$", text, re.M)
    assert start is not None, f"no `  {name}:` key"
    rest = text[start.end() :]
    following = re.search(r"^  [A-Za-z0-9_-]+:\s*$", rest, re.M)
    return rest[: following.start()] if following else rest


MADE_UP_YAML = """\
jobs:
  api:
    env:
      LLM_PROVIDER: fake
    steps:
      - run: echo api
  web:
    env:
      OTHER: 1
  smoke:
    env:
      LLM_PROVIDER: fake
"""


def test_a_section_stops_at_the_next_key_of_the_same_indent():
    api = job_section(MADE_UP_YAML, "api")
    assert "LLM_PROVIDER: fake" in api
    assert "echo api" in api
    assert "OTHER" not in api  # web's text is not in api's
    assert "smoke" not in api


def test_a_middle_section_does_not_include_the_next_one():
    web = job_section(MADE_UP_YAML, "web")
    assert "OTHER: 1" in web
    assert "LLM_PROVIDER" not in web  # smoke's line is not in web's


def test_the_last_section_runs_to_the_end_and_a_missing_key_fails():
    assert "LLM_PROVIDER: fake" in job_section(MADE_UP_YAML, "smoke")
    with pytest.raises(AssertionError):
        job_section(MADE_UP_YAML, "docker")


def test_env_example_sets_the_fake_provider_and_an_empty_key():
    lines = read(ENV_EXAMPLE).splitlines()
    assert "LLM_PROVIDER=fake" in lines
    assert "LLM_API_KEY=" in lines  # empty: a clean clone needs no key


def test_env_example_explains_when_the_key_is_required():
    lines = read(ENV_EXAMPLE).splitlines()
    assert "LLM_API_KEY=" in lines
    i = lines.index("LLM_API_KEY=")
    comment = " ".join(lines[max(0, i - 4) : i])
    assert re.search(r"required", comment, re.I)
    assert "fake" in comment


def test_a_clean_clone_boots_from_the_env_example(no_llm_env):
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.llm_provider == "fake"
    assert settings.fake_llm_mode == "ok"


def test_compose_requires_the_provider_and_passes_the_key():
    api = job_section(read(COMPOSE), "api")
    assert "LLM_PROVIDER: ${LLM_PROVIDER:?set it in .env}" in api
    assert re.search(r"^\s+LLM_API_KEY: \$\{LLM_API_KEY:-\}$", api, re.M)


def test_compose_does_not_default_the_provider():
    assert "LLM_PROVIDER:-" not in read(COMPOSE)


def test_the_ci_api_job_sets_the_fake_provider():
    api_job = job_section(read(CI), "api")
    assert re.search(r"^\s+LLM_PROVIDER: fake$", api_job, re.M)


def test_ci_has_no_llm_key():
    assert "LLM_API_KEY" not in read(CI)  # no GitHub Actions secrets in Phase 1 (ADR-0003 D6)
