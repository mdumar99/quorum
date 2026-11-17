"""The normalized errors and the adapter-side ProviderFailure (ADR-0003 D5; ADR-0004 D2; #48)."""

import pytest
from llm_helpers import PROVIDER_ERROR_TYPES, TRANSIENT, schema_error_types

from app.llm import (
    InvalidLLMOutputError,
    LLMCallRecord,
    LLMConfigError,
    LLMError,
    LLMTimeoutError,
    PermanentLLMError,
    ProviderFailure,
    ProviderUnavailableError,
    RateLimitedError,
    TransientLLMError,
)

ERRORS = {
    "llm_timeout": LLMTimeoutError,
    "rate_limited": RateLimitedError,
    "provider_unavailable": ProviderUnavailableError,
    "invalid_llm_output": InvalidLLMOutputError,
}


def make_record() -> LLMCallRecord:
    return LLMCallRecord(provider="fake", model_id="fake-v1", prompt_version="pv-1", latency_ms=1)


def test_each_error_type_is_one_of_the_values_the_schema_allows():
    allowed = schema_error_types()
    assert allowed >= set(PROVIDER_ERROR_TYPES)
    for cls in ERRORS.values():
        assert cls.error_type in allowed


def test_the_client_defines_exactly_the_four_provider_errors():
    assert {cls.error_type for cls in ERRORS.values()} == set(PROVIDER_ERROR_TYPES)
    assert {"capacity", "abandoned"} <= schema_error_types()  # in the schema, not defined here
    assert not {"capacity", "abandoned"} & {cls.error_type for cls in ERRORS.values()}


@pytest.mark.parametrize("error_type", sorted(ERRORS))
def test_transient_and_permanent_follow_the_adr(error_type):
    cls = ERRORS[error_type]
    assert issubclass(cls, LLMError)
    assert cls.transient is (error_type in TRANSIENT)
    assert issubclass(cls, TransientLLMError) is (error_type in TRANSIENT)
    assert issubclass(cls, PermanentLLMError) is (error_type not in TRANSIENT)


@pytest.mark.parametrize("cls", ERRORS.values(), ids=ERRORS.keys())
def test_a_failed_call_carries_its_record(cls):
    record = make_record()
    err = cls("it failed", record)
    assert err.record is record
    assert "it failed" in str(err)


def test_a_rate_limit_error_keeps_retry_after_as_a_float_as_given():
    err = RateLimitedError("slow down", make_record(), retry_after_seconds=7.5)
    assert err.retry_after_seconds == 7.5
    assert isinstance(err.retry_after_seconds, float)


def test_a_rate_limit_error_without_retry_after_has_none():
    assert RateLimitedError("slow down", make_record()).retry_after_seconds is None


def test_a_config_error_is_not_an_llm_error():
    assert not issubclass(LLMConfigError, LLMError)


@pytest.mark.parametrize("error_type", PROVIDER_ERROR_TYPES)
def test_a_provider_failure_accepts_the_four_provider_error_types(error_type):
    failure = ProviderFailure(error_type, raw_output="x", retry_after_seconds=1.5)
    assert failure.error_type == error_type
    assert failure.raw_output == "x"
    assert failure.retry_after_seconds == 1.5


@pytest.mark.parametrize("error_type", ["capacity", "abandoned", "nonsense", ""])
def test_a_provider_failure_rejects_any_other_error_type(error_type):
    with pytest.raises(ValueError):
        ProviderFailure(error_type)
