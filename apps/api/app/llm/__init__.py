"""The LLM client: interface, fake adapter, normalized errors and the raw-output capture."""

from app.llm.capture import MAX_RAW_OUTPUT_BYTES, capture_raw_output
from app.llm.client import LLMClient
from app.llm.errors import (
    InvalidLLMOutputError,
    LLMConfigError,
    LLMError,
    LLMTimeoutError,
    PermanentLLMError,
    ProviderFailure,
    ProviderUnavailableError,
    RateLimitedError,
    TransientLLMError,
)
from app.llm.factory import build_llm_client
from app.llm.fake import (
    FAKE_LLM_MODES,
    FakeFailure,
    FakeLLMClient,
    FakeReply,
    FakeScriptExhausted,
)
from app.llm.types import LLMCallRecord, LLMRequest, LLMResult, ProviderReply, RawOutputCapture

__all__ = [
    "FAKE_LLM_MODES",
    "MAX_RAW_OUTPUT_BYTES",
    "FakeFailure",
    "FakeLLMClient",
    "FakeReply",
    "FakeScriptExhausted",
    "InvalidLLMOutputError",
    "LLMCallRecord",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMRequest",
    "LLMResult",
    "LLMTimeoutError",
    "PermanentLLMError",
    "ProviderFailure",
    "ProviderReply",
    "ProviderUnavailableError",
    "RateLimitedError",
    "RawOutputCapture",
    "TransientLLMError",
    "build_llm_client",
    "capture_raw_output",
]
