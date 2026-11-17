"""The fake adapter (ADR-0003, Decisions 5 and 8; issue #48).

It carries CI, the unit tests and the smoke test, so no key and no model are needed. It never
looks at the prompt: a test picks an outcome explicitly, either as a scripted sequence (unit
tests) or as a mode (`FAKE_LLM_MODE`, the Compose and smoke-test path). It never sleeps: a
`llm_timeout` is raised at once, and the base class's deadline is tested with its own adapter.
"""

from collections import deque
from collections.abc import Sequence
from typing import ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict

from app.llm.client import LLMClient
from app.llm.errors import ProviderFailure
from app.llm.types import LLMRequest, ProviderReply

FakeMode = Literal[
    "ok", "llm_timeout", "rate_limited", "provider_unavailable", "invalid_llm_output"
]
# "ok" is first: it is the default.
FAKE_LLM_MODES: tuple[str, ...] = get_args(FakeMode)

_OK_TEXT = "fake-llm: placeholder output (P1-12 makes it schema-valid)"
_INVALID_TEXT = "fake-llm: this is not a decision"
_RETRY_AFTER_SECONDS = 1.0


class FakeScriptExhausted(Exception):
    """The script ran out. Deliberately not an LLMError: a test bug, not a provider failure."""


class FakeReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class FakeFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error_type: Literal["llm_timeout", "rate_limited", "provider_unavailable", "invalid_llm_output"]
    raw_output: str | None = None
    retry_after_seconds: float | None = None


_MODE_OUTCOMES: dict[str, FakeReply | FakeFailure] = {
    "ok": FakeReply(text=_OK_TEXT),
    "llm_timeout": FakeFailure(error_type="llm_timeout"),
    "rate_limited": FakeFailure(
        error_type="rate_limited", retry_after_seconds=_RETRY_AFTER_SECONDS
    ),
    "provider_unavailable": FakeFailure(error_type="provider_unavailable"),
    "invalid_llm_output": FakeFailure(error_type="invalid_llm_output", raw_output=_INVALID_TEXT),
}


class FakeLLMClient(LLMClient):
    provider: ClassVar[str] = "fake"
    model_id: ClassVar[str] = "fake-v1"

    def __init__(
        self,
        script: Sequence[FakeReply | FakeFailure] | None = None,
        mode: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        """Give a `script` (one outcome per call, then it raises) or a `mode` (repeats forever)."""
        super().__init__(timeout_seconds)
        if script is not None and mode is not None:
            raise ValueError("give a script or a mode, not both")
        if mode is not None and mode not in FAKE_LLM_MODES:
            raise ValueError(f"mode must be one of: {', '.join(FAKE_LLM_MODES)}")
        self.mode = None if script is not None else (mode or "ok")
        self._script = deque(script) if script is not None else None

    def _next_outcome(self) -> FakeReply | FakeFailure:
        if self._script is None:
            return _MODE_OUTCOMES[self.mode]
        if not self._script:
            raise FakeScriptExhausted("the fake's script has no outcome left for this call")
        return self._script.popleft()

    async def _call(self, request: LLMRequest) -> ProviderReply:
        outcome = self._next_outcome()
        if isinstance(outcome, FakeReply):
            return ProviderReply(
                text=outcome.text,
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
            )
        raise ProviderFailure(
            outcome.error_type,
            f"fake: {outcome.error_type}",
            raw_output=outcome.raw_output,
            retry_after_seconds=outcome.retry_after_seconds,
        )
