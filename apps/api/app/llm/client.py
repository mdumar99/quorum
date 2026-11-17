"""The LLM client base class: it owns the deadline, the timing and the capture (issue #48).

An adapter implements only `_call`. `complete` does the rest, so no adapter (the fake now, Groq in
P1-14) can forget the deadline, skip the timing, store the raw output uncapped or retry:

- the deadline is `asyncio.timeout`; the call is never retried (ADR-0003, Decision 5);
- latency is whole milliseconds from `perf_counter_ns`, measured on success and on failure;
- a `ProviderFailure` from the adapter becomes the matching `LLMError`, carrying the call record;
- any other exception from `_call` is a bug in the adapter and propagates unchanged, and
  `CancelledError` passes through, so a cancelled request is never reported as a timeout.
"""

import asyncio
import math
import time
from abc import ABC, abstractmethod

from app.llm.capture import capture_raw_output
from app.llm.errors import (
    ERROR_CLASSES,
    LLMError,
    LLMTimeoutError,
    ProviderFailure,
    RateLimitedError,
)
from app.llm.types import LLMCallRecord, LLMRequest, LLMResult, ProviderReply


class LLMClient(ABC):
    provider: str
    model_id: str

    def __init__(self, timeout_seconds: float) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive and finite, got {timeout_seconds}")
        self.timeout_seconds = timeout_seconds

    async def complete(self, request: LLMRequest) -> LLMResult:
        started = time.perf_counter_ns()
        deadline = asyncio.timeout(self.timeout_seconds)
        try:
            async with deadline:
                reply = await self._call(request)
        except ProviderFailure as failure:
            record = self._record(
                request,
                started,
                raw_output=failure.raw_output,
                prompt_tokens=failure.prompt_tokens,
                completion_tokens=failure.completion_tokens,
            )
            raise self._error_for(failure, record) from failure
        except TimeoutError as exc:
            if not deadline.expired():
                raise  # the adapter's own TimeoutError is not our deadline: propagate unchanged
            record = self._record(request, started)
            message = f"the {self.provider} call exceeded its {self.timeout_seconds} s deadline"
            raise LLMTimeoutError(message, record) from exc
        record = self._record(
            request,
            started,
            raw_output=reply.text,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )
        return LLMResult(text=reply.text, record=record)

    @abstractmethod
    async def _call(self, request: LLMRequest) -> ProviderReply:
        """The adapter's own call. It raises `ProviderFailure` for a provider error."""

    def _record(
        self,
        request: LLMRequest,
        started_ns: int,
        *,
        raw_output: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> LLMCallRecord:
        return LLMCallRecord(
            provider=self.provider,
            model_id=self.model_id,
            prompt_version=request.prompt_version,
            latency_ms=(time.perf_counter_ns() - started_ns) // 1_000_000,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            raw_output=capture_raw_output(raw_output) if raw_output is not None else None,
        )

    def _error_for(self, failure: ProviderFailure, record: LLMCallRecord) -> LLMError:
        message = f"{self.provider} call failed: {failure}"
        error_class = ERROR_CLASSES[failure.error_type]
        if error_class is RateLimitedError:
            return RateLimitedError(message, record, failure.retry_after_seconds)
        return error_class(message, record)
