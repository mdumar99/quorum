"""The three pool settings: names, limits and defaults (issue #50).

Every test builds Settings with `_env_file=None` and a clean environment, so a developer's own .env
cannot decide a result. The size defaults are chosen from the measured runs, so no test pins them
here: they are only required to be a valid pool.
"""

import math

import pytest
from db_helpers import make_settings
from pydantic import ValidationError

pytestmark = pytest.mark.usefixtures("no_pool_env")


def locs(excinfo) -> list[tuple]:
    return [e["loc"] for e in excinfo.value.errors()]


def test_the_default_sizes_are_a_valid_pool():
    settings = make_settings()
    assert settings.db_pool_min_size >= 1
    assert settings.db_pool_max_size >= settings.db_pool_min_size


def test_the_checkout_timeout_defaults_to_5_seconds():
    settings = make_settings()
    assert settings.db_pool_checkout_timeout_seconds == 5.0
    assert isinstance(settings.db_pool_checkout_timeout_seconds, float)


@pytest.mark.parametrize("value", [0, -1])
def test_a_min_size_below_one_is_rejected(value):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(db_pool_min_size=value, db_pool_max_size=4)
    assert ("db_pool_min_size",) in locs(excinfo)


@pytest.mark.parametrize("value", [0, -1])
def test_a_max_size_below_one_is_rejected(value):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(db_pool_min_size=1, db_pool_max_size=value)
    assert ("db_pool_max_size",) in locs(excinfo)


def test_a_min_size_above_the_max_size_is_rejected_and_both_names_are_in_the_error():
    with pytest.raises(ValidationError) as excinfo:
        make_settings(db_pool_min_size=5, db_pool_max_size=4)
    text = str(excinfo.value)
    assert "db_pool_min_size" in text
    assert "db_pool_max_size" in text


@pytest.mark.parametrize(("low", "high"), [(1, 1), (4, 4), (1, 4), (2, 4)])
def test_a_min_size_not_above_the_max_size_is_accepted(low, high):
    settings = make_settings(db_pool_min_size=low, db_pool_max_size=high)
    assert (settings.db_pool_min_size, settings.db_pool_max_size) == (low, high)


@pytest.mark.parametrize(
    ("field", "others"),
    [("db_pool_min_size", {}), ("db_pool_max_size", {"db_pool_min_size": 1})],
    ids=["db_pool_min_size", "db_pool_max_size"],
)
def test_a_size_must_be_a_whole_number(field, others):
    """2.5 must be refused by the whole-number rule alone. For the max, the min is set to 1 so that
    the min-above-max rule (the default min of 4 is above 2.5) cannot be what refuses it."""
    with pytest.raises(ValidationError) as excinfo:
        make_settings(**{**others, field: 2.5})
    assert (field,) in locs(excinfo)


@pytest.mark.parametrize(
    "value",
    [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan],
    ids=["zero", "zero-float", "minus-1", "minus-half", "inf", "minus-inf", "nan"],
)
def test_the_checkout_timeout_must_be_positive_and_finite(value):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(db_pool_checkout_timeout_seconds=value)
    assert ("db_pool_checkout_timeout_seconds",) in locs(excinfo)


def test_the_checkout_timeout_may_be_a_fraction_of_a_second():
    settings = make_settings(db_pool_checkout_timeout_seconds=0.05)
    assert settings.db_pool_checkout_timeout_seconds == 0.05


@pytest.mark.parametrize("text", ["inf", "-inf", "nan", "0", "-3"])
def test_a_bad_checkout_timeout_from_the_environment_is_rejected(monkeypatch, text):
    monkeypatch.setenv("DB_POOL_CHECKOUT_TIMEOUT_SECONDS", text)
    with pytest.raises(ValidationError) as excinfo:
        make_settings()
    assert ("db_pool_checkout_timeout_seconds",) in locs(excinfo)


def test_the_three_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    monkeypatch.setenv("DB_POOL_CHECKOUT_TIMEOUT_SECONDS", "1.5")
    settings = make_settings()
    assert settings.db_pool_min_size == 2
    assert settings.db_pool_max_size == 3
    assert settings.db_pool_checkout_timeout_seconds == 1.5
