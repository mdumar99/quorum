"""The base client: its own deadline, no retries, latency, capture and error mapping (#48).

The adapters here are tiny test doubles. They show what the BASE class does for any adapter, so a
future adapter (Groq, P1-14) cannot forget the deadline, the timing or the capture.
"""

import asyncio
import math

import pytest

from app.llm import (
    InvalidLLMOutputError,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMTimeoutError,
    ProviderFailure,
    ProviderReply,
    ProviderUnavailableError,
    RateLimitedError,
)

REQUEST = LLMRequest(prompt="a finished prompt", prompt_version="pv-1")


class Adapter(LLMClient):
    provider = "test"
    model_id = "test-model"

    def __init__(self, timeout_seconds: float = 5.0, *, reply=None, failure=None, delay=0.0):
        super().__init__(timeout_seconds)
        self.reply = reply
        self.failure = failure
        self.delay = delay
        self.calls = 0

    async def _call(self, request: LLMRequest) -> ProviderReply:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return self.reply


class Hanging(Adapter):
    async def _call(self, request: LLMRequest) -> ProviderReply:
        self.calls += 1
        await asyncio.sleep(5)  # far longer than any test deadline: only the client ends this


class OwnTimeout(Adapter):
    """Raises a TimeoutError of its own (a socket timeout, say), long before any deadline."""

    async def _call(self, request: LLMRequest) -> ProviderReply:
        self.calls += 1
        raise TimeoutError("the adapter's own timeout")


def complete(client: LLMClient, request: LLMRequest = REQUEST):
    return asyncio.run(client.complete(request))


def test_success_returns_the_full_text_and_a_record():
    client = Adapter(reply=ProviderReply(text="hello", prompt_tokens=7, completion_tokens=3))
    result = complete(client)
    assert result.text == "hello"
    rec = result.record
    assert (rec.provider, rec.model_id, rec.prompt_version) == ("test", "test-model", "pv-1")
    assert (rec.prompt_tokens, rec.completion_tokens) == (7, 3)
    assert rec.raw_output is not None
    assert rec.raw_output.text == "hello"
    assert client.calls == 1


def test_tokens_may_be_missing():
    result = complete(Adapter(reply=ProviderReply(text="hello")))
    assert result.record.prompt_tokens is None
    assert result.record.completion_tokens is None


def test_only_the_stored_copy_is_capped_the_result_keeps_the_full_text():
    result = complete(Adapter(reply=ProviderReply(text="a" * 20_000)))
    assert result.text == "a" * 20_000
    assert result.record.raw_output.text == "a" * 16_384
    assert result.record.raw_output.original_bytes == 20_000
    assert result.record.raw_output.truncated is True


def test_a_nul_in_the_output_is_captured_not_rejected():
    result = complete(Adapter(reply=ProviderReply(text="a\x00b")))
    assert result.text == "a\x00b"
    assert result.record.raw_output.text == "a\x1ab"
    assert result.record.raw_output.original_bytes == 3


def test_latency_is_whole_milliseconds_and_covers_the_call():
    result = complete(Adapter(reply=ProviderReply(text="x"), delay=0.05))
    assert isinstance(result.record.latency_ms, int)
    assert 40 <= result.record.latency_ms < 5000


def test_the_client_has_its_own_deadline():
    client = Hanging(timeout_seconds=0.05)
    with pytest.raises(LLMTimeoutError) as caught:
        complete(client)
    rec = caught.value.record
    assert rec.latency_ms >= 40  # the failed call is timed too
    assert (rec.provider, rec.model_id, rec.prompt_version) == ("test", "test-model", "pv-1")
    assert rec.raw_output is None
    assert client.calls == 1  # no retry after the deadline


def test_the_deadline_is_the_clients_setting_not_a_constant():
    client = Adapter(timeout_seconds=0.05, reply=ProviderReply(text="slow"), delay=0.5)
    with pytest.raises(LLMTimeoutError):
        complete(client)
    fast = Adapter(timeout_seconds=5.0, reply=ProviderReply(text="ok"), delay=0.05)
    assert complete(fast).text == "ok"


@pytest.mark.parametrize(
    ("failure", "error_class"),
    [
        (ProviderFailure("llm_timeout"), LLMTimeoutError),
        (ProviderFailure("rate_limited", retry_after_seconds=7.5), RateLimitedError),
        (ProviderFailure("provider_unavailable"), ProviderUnavailableError),
        (ProviderFailure("invalid_llm_output", raw_output="nope"), InvalidLLMOutputError),
    ],
    ids=["timeout", "rate-limited", "unavailable", "invalid"],
)
def test_a_provider_failure_is_raised_once_as_the_matching_error_with_no_retry(
    failure, error_class
):
    client = Adapter(failure=failure)
    with pytest.raises(error_class) as caught:
        complete(client)
    assert type(caught.value) is error_class
    assert client.calls == 1  # the client does not call again (ADR-0003, Decision 5)
    assert isinstance(caught.value.record.latency_ms, int)


def test_the_retry_after_of_the_provider_is_carried_as_given():
    client = Adapter(failure=ProviderFailure("rate_limited", retry_after_seconds=7.5))
    with pytest.raises(RateLimitedError) as caught:
        complete(client)
    assert caught.value.retry_after_seconds == 7.5


def test_a_rate_limit_without_retry_after_carries_none():
    with pytest.raises(RateLimitedError) as caught:
        complete(Adapter(failure=ProviderFailure("rate_limited")))
    assert caught.value.retry_after_seconds is None


def test_a_failed_call_still_carries_the_capped_raw_output_and_the_tokens():
    failure = ProviderFailure(
        "invalid_llm_output",
        raw_output="bad\x00" + "x" * 20_000,
        prompt_tokens=9,
        completion_tokens=2,
    )
    with pytest.raises(InvalidLLMOutputError) as caught:
        complete(Adapter(failure=failure))
    rec = caught.value.record
    assert rec.raw_output is not None
    assert rec.raw_output.text.startswith("bad\x1a")
    assert rec.raw_output.original_bytes == 4 + 20_000
    assert rec.raw_output.truncated is True
    assert (rec.prompt_tokens, rec.completion_tokens) == (9, 2)


def test_a_failure_without_output_has_no_raw_output_in_its_record():
    with pytest.raises(ProviderUnavailableError) as caught:
        complete(Adapter(failure=ProviderFailure("provider_unavailable")))
    assert caught.value.record.raw_output is None


def test_an_unexpected_exception_propagates_with_its_own_type():
    """Not a ProviderFailure: a bug in an adapter is not a provider error (decided: no mapping)."""
    client = Adapter(failure=KeyError("boom"))
    with pytest.raises(KeyError) as caught:
        complete(client)
    assert not isinstance(caught.value, LLMError)
    assert client.calls == 1


def test_cancelling_the_task_raises_cancelled_error_and_is_not_a_timeout():
    async def scenario():
        client = Hanging(timeout_seconds=30.0)
        task = asyncio.create_task(client.complete(REQUEST))
        await asyncio.sleep(0.05)  # let the call start and block
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value",
    [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan],
    ids=["zero", "zero-float", "minus-1", "minus-half", "inf", "minus-inf", "nan"],
)
def test_the_client_rejects_a_timeout_that_is_not_positive_and_finite(value):
    with pytest.raises(ValueError):
        Adapter(timeout_seconds=value)


def test_the_client_accepts_a_small_positive_timeout():
    assert Adapter(timeout_seconds=0.05).timeout_seconds == 0.05


def test_an_adapters_own_timeout_error_is_not_the_clients_deadline():
    """The deadline is 30 s and never fires: a TimeoutError from _call is the adapter's bug."""
    client = OwnTimeout(timeout_seconds=30.0)
    with pytest.raises(TimeoutError) as caught:
        complete(client)
    assert not isinstance(caught.value, LLMTimeoutError)
    assert client.calls == 1
