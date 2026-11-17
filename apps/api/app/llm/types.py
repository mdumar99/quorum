"""The data the LLM client takes and returns (ADR-0003, Decisions 5 and 10; issue #48).

The models mirror the schema's CHECKs on the ten LLM columns of `audit_log` and `decisions`
(migration 0002), so a record that validates here is one the database accepts.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ADR-0003, Decision 10: at most 16 KiB of UTF-8 is stored. Defined here, not in capture.py,
# because RawOutputCapture checks it and capture.py imports this module.
MAX_RAW_OUTPUT_BYTES = 16_384


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LLMRequest(_Frozen):
    """A finished prompt. The caller supplies `prompt_version`: the client never builds prompts."""

    prompt: str
    # Non-empty because the schema needs it wherever a provider is recorded (audit_log_llm_call_ck).
    prompt_version: str = Field(min_length=1)


class RawOutputCapture(_Frozen):
    """The capped raw output and what the schema's raw-output CHECKs need (ADR-0003 D10).

    `text` is what is stored: it holds no NUL and no lone surrogate (Postgres text refuses the
    first and UTF-8 cannot encode the second), and it is at most 16,384 bytes. `original_bytes`
    and `sha256` are of the output as the model sent it, before any replacement or cut.
    """

    text: str
    original_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool

    @model_validator(mode="after")
    def _text_and_lengths_agree(self) -> "RawOutputCapture":
        if "\x00" in self.text:
            raise ValueError("the stored text contains a NUL character")
        try:
            stored_bytes = len(self.text.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("the stored text contains a lone surrogate") from None
        if stored_bytes > MAX_RAW_OUTPUT_BYTES:
            raise ValueError(f"the stored text is over {MAX_RAW_OUTPUT_BYTES} bytes")
        if self.original_bytes < stored_bytes:
            raise ValueError("original_bytes is below the stored byte length")
        if self.truncated != (self.original_bytes > stored_bytes):
            raise ValueError("truncated must equal (original_bytes > stored byte length)")
        return self


class ProviderReply(_Frozen):
    """What an adapter's `_call` returns: the full text and the token counts, if given."""

    text: str
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class LLMCallRecord(_Frozen):
    """One call, as the ten LLM columns of `audit_log` and `decisions` hold it."""

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    latency_ms: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    raw_output: RawOutputCapture | None = None

    def columns(self) -> dict[str, object]:
        """The ten column values, by column name; the four raw-output ones are null together."""
        capture = self.raw_output
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "raw_output": capture.text if capture else None,
            "raw_output_bytes": capture.original_bytes if capture else None,
            "raw_output_sha256": capture.sha256 if capture else None,
            "raw_output_truncated": capture.truncated if capture else None,
        }


class LLMResult(_Frozen):
    """A successful call: the full original text (for the parser) and the record (capped)."""

    text: str
    record: LLMCallRecord
