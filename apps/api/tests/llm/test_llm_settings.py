"""The LLM settings: provider, key, timeout and fake mode (ADR-0003 D6; #48 decisions 1 and 2).

Every test builds Settings with `_env_file=None` and a clean environment (`no_llm_env`), so a
developer's own root .env, which will hold LLM_PROVIDER, cannot decide any result.
"""

import json
import math

import pytest
from llm_helpers import PROVIDER_ERROR_TYPES
from pydantic import SecretStr, ValidationError

from app.config import Settings

KEY = "KEYMARK12345"  # short, so a truncated error message would still show all of it
URLS = {"database_url": "postgresql://unused", "redis_url": "redis://unused"}
pytestmark = pytest.mark.usefixtures("no_llm_env")


def make(**kwargs) -> Settings:
    return Settings(_env_file=None, **{**URLS, **kwargs})


def secret(settings: Settings) -> str:
    assert isinstance(settings.llm_api_key, SecretStr), type(settings.llm_api_key)
    return settings.llm_api_key.get_secret_value()


def locs(excinfo) -> list[tuple]:
    return [e["loc"] for e in excinfo.value.errors()]


# ---------- the provider ----------


def test_llm_provider_has_no_default():
    with pytest.raises(ValidationError) as excinfo:
        make()
    assert ("llm_provider",) in locs(excinfo)
    assert any(e["type"] == "missing" for e in excinfo.value.errors())


@pytest.mark.parametrize("value", ["fake", "groq"])
def test_the_two_accepted_providers(value):
    assert make(llm_provider=value, llm_api_key=KEY).llm_provider == value


@pytest.mark.parametrize("value", ["openai", "", "FAKE", "Groq", " fake"])
def test_an_unknown_provider_is_rejected(value):
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider=value, llm_api_key=KEY)
    assert ("llm_provider",) in locs(excinfo)


def test_the_provider_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert make().llm_provider == "fake"


def test_a_root_env_file_is_ignored_when_the_test_turns_it_off(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=fake\n")
    with pytest.raises(ValidationError):  # make() passes _env_file=None
        make()
    assert Settings(_env_file=env_file, **URLS).llm_provider == "fake"  # and it IS read if given


# ---------- the key ----------


def test_the_key_is_optional_for_fake():
    assert make(llm_provider="fake").llm_api_key is None


def test_an_empty_key_is_accepted_for_fake():
    """.env.example ships `LLM_API_KEY=` empty, and a clean clone must boot with it."""
    assert make(llm_provider="fake", llm_api_key="").llm_provider == "fake"


@pytest.mark.parametrize("key", [None, ""], ids=["missing", "empty"])
def test_groq_without_a_key_fails_validation(key):
    kwargs = {"llm_provider": "groq"}
    if key is not None:
        kwargs["llm_api_key"] = key
    with pytest.raises(ValidationError) as excinfo:
        make(**kwargs)
    assert "llm_api_key" in str(excinfo.value)


def test_groq_with_a_key_builds_the_settings():
    assert secret(make(llm_provider="groq", llm_api_key=KEY)) == KEY


def test_the_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", KEY)
    assert secret(make()) == KEY


def test_the_key_is_a_secret_str():
    key = make(llm_provider="fake", llm_api_key=KEY).llm_api_key
    assert isinstance(key, SecretStr)


@pytest.mark.parametrize("provider", ["fake", "groq"])
def test_the_key_value_never_appears_in_repr_str_or_dumps(provider):
    settings = make(llm_provider=provider, llm_api_key=KEY)
    assert KEY not in repr(settings)
    assert KEY not in str(settings)
    assert KEY not in str(settings.model_dump())
    assert KEY not in settings.model_dump_json()
    assert KEY not in json.dumps(settings.model_dump(mode="json"))


def test_a_failing_settings_error_does_not_contain_the_key():
    """The maintainer's case: provider groq, a key, and another field wrong."""
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider="groq", llm_api_key="sk-test-secret", log_level="LOUD")
    exc = excinfo.value
    for text in (str(exc), repr(exc), exc.json(), str(exc.errors())):
        assert "sk-test-secret" not in text
        assert "sk-test" not in text


DB_PASSWORD = "Zq7xPw41Km"  # made up, distinctive; it appears nowhere else


def assert_error_text_has_no_input(exc: ValidationError, secret: str) -> None:
    """The text of a validation error (str and repr: what a traceback or a log line shows).

    Three checks, each a boolean so a failure message can never print the secret:
    - the secret is absent;
    - its first 8 characters are absent (pydantic cuts a long input in the middle, so a partial
      secret can leak while the whole one does not);
    - no `input_value` or `input_type` is printed at all. That one is structural: it must hold
      whatever the input looks like, long or short.

    `.errors()` and `.json()` still return the input unless the caller passes
    include_input=False; the app never calls them on a Settings error (docs/llm.md).
    """
    for text in (str(exc), repr(exc)):
        whole = secret in text
        prefix = secret[:8] in text
        shows_input = "input_value" in text or "input_type" in text
        assert not whole, "the secret is in the text of the error"
        assert not prefix, "the first 8 characters of the secret are in the text of the error"
        assert not shows_input, "the text of the error prints the input (input_value or input_type)"


def test_a_missing_field_error_does_not_echo_the_input_with_the_key():
    """The key is given as a keyword; DATABASE_URL, REDIS_URL and LLM_PROVIDER are missing.

    The input is just the key, so pydantic would print it whole if the input were not hidden.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, llm_api_key=KEY)
    assert ("database_url",) in locs(excinfo)
    assert_error_text_has_no_input(excinfo.value, KEY)


def test_a_missing_field_error_does_not_echo_a_database_url_password():
    """redis_url is missing: the error must not print the input, which holds the database URL."""
    url = f"postgresql://u:{DB_PASSWORD}@h/d"
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, database_url=url)
    assert ("redis_url",) in locs(excinfo)
    assert_error_text_has_no_input(excinfo.value, DB_PASSWORD)


def test_an_error_does_not_echo_a_key_that_came_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)  # DATABASE_URL, REDIS_URL and LLM_PROVIDER are missing
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert ("database_url",) in locs(excinfo)
    assert_error_text_has_no_input(excinfo.value, KEY)


@pytest.mark.parametrize("key", ["   ", "\t", " \n "], ids=["spaces", "tab", "space-newline"])
def test_groq_with_a_whitespace_only_key_fails_validation(key):
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider="groq", llm_api_key=key)
    assert "llm_api_key" in str(excinfo.value)


def test_groq_with_a_whitespace_only_key_from_the_environment_fails_validation(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "   ")
    with pytest.raises(ValidationError) as excinfo:
        make()
    assert "llm_api_key" in str(excinfo.value)


@pytest.mark.parametrize("key", ["   ", "\t", " \n "], ids=["spaces", "tab", "space-newline"])
def test_fake_with_a_whitespace_only_key_is_accepted(key):
    assert make(llm_provider="fake", llm_api_key=key).llm_provider == "fake"


# ---------- the timeout ----------


def test_the_llm_timeout_defaults_to_20_seconds():
    settings = make(llm_provider="fake")
    assert settings.llm_timeout_seconds == 20
    assert isinstance(settings.llm_timeout_seconds, float)


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_the_llm_timeout_must_be_positive(value):
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider="fake", llm_timeout_seconds=value)
    assert ("llm_timeout_seconds",) in locs(excinfo)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan], ids=["inf", "minus-inf", "nan"])
def test_the_llm_timeout_must_be_finite(value):
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider="fake", llm_timeout_seconds=value)
    assert ("llm_timeout_seconds",) in locs(excinfo)


@pytest.mark.parametrize("text", ["inf", "-inf", "nan"])
def test_the_llm_timeout_from_the_environment_must_be_finite(monkeypatch, text):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", text)
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider="fake")
    assert ("llm_timeout_seconds",) in locs(excinfo)


def test_the_llm_timeout_may_be_a_fraction_of_a_second():
    assert make(llm_provider="fake", llm_timeout_seconds=0.05).llm_timeout_seconds == 0.05


def test_the_llm_timeout_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "5")
    assert make(llm_provider="fake").llm_timeout_seconds == 5.0


# ---------- the fake's mode ----------


def test_the_fake_mode_defaults_to_ok():
    assert make(llm_provider="fake").fake_llm_mode == "ok"


@pytest.mark.parametrize("mode", ["ok", *PROVIDER_ERROR_TYPES])
def test_every_fake_mode_is_accepted(mode):
    assert make(llm_provider="fake", fake_llm_mode=mode).fake_llm_mode == mode


@pytest.mark.parametrize("mode", ["bogus", "", "OK", "capacity"])
def test_an_unknown_fake_mode_is_rejected(mode):
    with pytest.raises(ValidationError) as excinfo:
        make(llm_provider="fake", fake_llm_mode=mode)
    assert ("fake_llm_mode",) in locs(excinfo)


def test_the_fake_mode_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FAKE_LLM_MODE", "rate_limited")
    assert make(llm_provider="fake").fake_llm_mode == "rate_limited"
