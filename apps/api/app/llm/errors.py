"""Normalized LLM errors (ADR-0003, Decision 5; ADR-0004, Decision 2; issue #48).

Transient: `llm_timeout`, `rate_limited`, `provider_unavailable`. Permanent: `invalid_llm_output`
(including refusals and truncation). `capacity` and `abandoned` are also allowed by the schema
but are not provider errors: the pipeline raises them (P1-12, P1-13), so they are not defined here.
"""

from typing import ClassVar

from app.llm.types import LLMCallRecord


class LLMConfigError(Exception):
    """The configured provider cannot be built (`groq` before P1-14). Not an LLMError."""


class LLMError(Exception):
    """A failed call. It carries the call record, so a failed attempt can still be audited."""

    error_type: ClassVar[str]
    transient: ClassVar[bool]

    def __init__(self, message: str, record: LLMCallRecord) -> None:
        super().__init__(message)
        self.record = record


class TransientLLMError(LLMError):
    transient = True


class PermanentLLMError(LLMError):
    transient = False


class LLMTimeoutError(TransientLLMError):
    error_type = "llm_timeout"


class RateLimitedError(TransientLLMError):
    error_type = "rate_limited"

    def __init__(
        self, message: str, record: LLMCallRecord, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message, record)
        # As the provider gave it. P1-12 rounds it up to an integer and caps it at 3600.
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailableError(TransientLLMError):
    error_type = "provider_unavailable"


class InvalidLLMOutputError(PermanentLLMError):
    error_type = "invalid_llm_output"


ERROR_CLASSES: dict[str, type[LLMError]] = {
    cls.error_type: cls
    for cls in (LLMTimeoutError, RateLimitedError, ProviderUnavailableError, InvalidLLMOutputError)
}


class ProviderFailure(Exception):
    """Raised by an adapter's `_call`; the client turns it into the matching `LLMError`.

    It is the adapter's side of the contract: it carries what the adapter knows (the error type,
    any raw output and token counts, the provider's retry-after), and the client adds the
    deadline, the timing and the record.
    """

    def __init__(
        self,
        error_type: str,
        message: str = "",
        *,
        raw_output: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        if error_type not in ERROR_CLASSES:
            allowed = ", ".join(sorted(ERROR_CLASSES))
            raise ValueError(f"error_type must be one of: {allowed}")
        super().__init__(message or error_type)
        self.error_type = error_type
        self.raw_output = raw_output
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.retry_after_seconds = retry_after_seconds
