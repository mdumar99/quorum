"""The dependency, .env.example and Compose for the pool (issue #50).

Text checks on the committed files: nothing runs Docker. Compose passes none of the three settings,
like LLM_TIMEOUT_SECONDS: the defaults apply, and docs/data-access.md says so.
"""

import re
import tomllib

from db_helpers import API_ROOT, REPO_ROOT

from app.config import Settings

POOL_SETTINGS = sorted(
    name.upper() for name in Settings.model_fields if name.startswith("db_pool_")
)


def read(path) -> str:
    assert path.is_file(), f"{path.name} not found"
    return path.read_text(encoding="utf-8")


def test_the_three_pool_settings_exist():
    assert POOL_SETTINGS == [
        "DB_POOL_CHECKOUT_TIMEOUT_SECONDS",
        "DB_POOL_MAX_SIZE",
        "DB_POOL_MIN_SIZE",
    ]


def test_psycopg_pool_is_declared_in_pyproject():
    dependencies = tomllib.loads(read(API_ROOT / "pyproject.toml"))["project"]["dependencies"]
    assert "psycopg-pool>=3.3.3" in dependencies


def test_psycopg_pool_is_locked_at_3_3_3_or_newer():
    packages = tomllib.loads(read(API_ROOT / "uv.lock"))["package"]
    locked = [p for p in packages if p["name"] == "psycopg-pool"]
    assert len(locked) == 1
    version = tuple(int(part) for part in locked[0]["version"].split("."))
    assert version >= (3, 3, 3)


def mentions_in_env_example(name: str) -> list[str]:
    return [line for line in read(REPO_ROOT / ".env.example").splitlines() if name in line]


def test_env_example_names_each_pool_setting_on_a_comment_line():
    """Optional settings, shown commented out with their defaults, like LLM_TIMEOUT_SECONDS."""
    for name in POOL_SETTINGS:
        mentions = mentions_in_env_example(name)
        assert mentions, f"{name} is not in .env.example"
        assert all(re.fullmatch(rf"# {name}=\S+", line) for line in mentions), name


def test_each_env_example_value_equals_the_default_in_settings():
    """Compared as numbers, so `5` and `5.0` are the same value."""
    for name in POOL_SETTINGS:
        mentions = mentions_in_env_example(name)
        assert len(mentions) == 1, f"{name} must appear exactly once in .env.example"
        match = re.fullmatch(rf"# {name}=(\S+)", mentions[0])
        assert match is not None, name
        assert float(match.group(1)) == float(Settings.model_fields[name.lower()].default), name


def test_compose_passes_none_of_the_pool_settings():
    assert "DB_POOL_" not in read(REPO_ROOT / "docker-compose.yml")
