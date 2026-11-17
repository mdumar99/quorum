"""The call record and its capture: shape, constraints and the ten schema columns (#48)."""

import pytest
from llm_helpers import LLM_COLUMNS, table_columns
from pydantic import ValidationError

from app.llm import LLMCallRecord, LLMRequest, LLMResult, RawOutputCapture

SHA = "ab" * 32


def capture(**overrides) -> RawOutputCapture:
    values = {"text": "hello", "original_bytes": 5, "sha256": SHA, "truncated": False}
    values.update(overrides)
    return RawOutputCapture(**values)


def record(**overrides) -> LLMCallRecord:
    values = {
        "provider": "fake",
        "model_id": "fake-v1",
        "prompt_version": "pv-1",
        "latency_ms": 12,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "raw_output": capture(),
    }
    values.update(overrides)
    return LLMCallRecord(**values)


def test_the_ten_columns_are_columns_of_audit_log_and_decisions():
    assert set(LLM_COLUMNS) <= table_columns("audit_log")
    assert set(LLM_COLUMNS) <= table_columns("decisions")


def test_columns_are_exactly_the_ten_schema_names():
    assert set(record().columns()) == set(LLM_COLUMNS)


def test_columns_map_the_record_and_the_capture():
    cols = record().columns()
    assert cols["provider"] == "fake"
    assert cols["model_id"] == "fake-v1"
    assert cols["prompt_version"] == "pv-1"
    assert cols["latency_ms"] == 12
    assert cols["prompt_tokens"] == 7
    assert cols["completion_tokens"] == 3
    assert cols["raw_output"] == "hello"
    assert cols["raw_output_bytes"] == 5
    assert cols["raw_output_sha256"] == SHA
    assert cols["raw_output_truncated"] is False


def test_a_record_without_output_has_all_four_raw_output_columns_null():
    cols = record(raw_output=None).columns()
    assert cols["raw_output"] is None
    assert cols["raw_output_bytes"] is None
    assert cols["raw_output_sha256"] is None
    assert cols["raw_output_truncated"] is None
    assert cols["provider"] == "fake"  # the call is still audited


def test_tokens_may_be_null():
    cols = record(prompt_tokens=None, completion_tokens=None).columns()
    assert cols["prompt_tokens"] is None
    assert cols["completion_tokens"] is None


@pytest.mark.parametrize("field", ["latency_ms", "prompt_tokens", "completion_tokens"])
def test_a_negative_number_is_rejected_like_the_schema_check(field):
    with pytest.raises(ValidationError):
        record(**{field: -1})


@pytest.mark.parametrize(
    "sha", ["ab" * 31, "AB" * 32, "zz" * 32, ""], ids=lambda s: s[:6] or "empty"
)
def test_a_sha256_that_is_not_64_lowercase_hex_is_rejected(sha):
    with pytest.raises(ValidationError):
        capture(sha256=sha)


def test_a_negative_original_length_is_rejected():
    with pytest.raises(ValidationError):
        capture(original_bytes=-1)


def test_the_types_are_frozen_and_closed():
    rec = record()
    with pytest.raises(ValidationError):
        rec.latency_ms = 99
    with pytest.raises(ValidationError):
        LLMRequest(prompt="p", prompt_version="v", extra="x")
    with pytest.raises(ValidationError):
        LLMResult(text="t", record=rec, extra="x")


def test_stored_text_of_exactly_the_limit_is_accepted():
    assert capture(text="a" * 16_384, original_bytes=16_384).truncated is False


@pytest.mark.parametrize(
    "text",
    ["a" * 16_385, "\u00e9" * 8193],
    ids=["ascii-16385-bytes", "2-byte-8193-chars-16386-bytes"],
)
def test_stored_text_over_16384_bytes_is_rejected(text):
    """Bytes, not characters: the second text is 8,193 characters but 16,386 bytes."""
    original = len(text.encode("utf-8"))
    with pytest.raises(ValidationError):
        capture(text=text, original_bytes=original, truncated=False)
    with pytest.raises(ValidationError):
        capture(text=text, original_bytes=original + 100, truncated=True)


@pytest.mark.parametrize(
    ("text", "original"),
    [("hello", 4), ("\u00e9", 1), ("a" * 100, 99)],
    ids=["ascii", "2-byte-char-counted-as-one", "long"],
)
def test_an_original_length_below_the_stored_byte_length_is_rejected(text, original):
    with pytest.raises(ValidationError):
        capture(text=text, original_bytes=original, truncated=False)


@pytest.mark.parametrize(
    ("original", "truncated"),
    [(5, True), (9, False), (6, False), (4097, False)],
    ids=["same-length-but-flagged", "longer-but-not-flagged", "one-longer-not-flagged", "big"],
)
def test_truncated_must_equal_original_longer_than_stored(original, truncated):
    with pytest.raises(ValidationError):
        capture(text="hello", original_bytes=original, truncated=truncated)


@pytest.mark.parametrize(
    ("text", "original", "truncated"),
    [("hello", 5, False), ("hello", 6, True), ("\u00e9", 2, False), ("", 0, False)],
    ids=["same", "one-longer", "2-byte-char", "empty"],
)
def test_consistent_captures_are_accepted(text, original, truncated):
    cap = capture(text=text, original_bytes=original, truncated=truncated)
    assert (cap.original_bytes, cap.truncated) == (original, truncated)


@pytest.mark.parametrize("field", ["provider", "model_id", "prompt_version"])
def test_an_empty_provider_model_or_prompt_version_is_rejected(field):
    with pytest.raises(ValidationError):
        record(**{field: ""})


def test_stored_text_containing_a_nul_is_rejected():
    """Postgres text cannot hold U+0000: the capture must have replaced it already."""
    with pytest.raises(ValidationError):
        capture(text="a\x00b", original_bytes=3, truncated=False)


def test_stored_text_containing_a_lone_surrogate_is_rejected():
    """UTF-8 cannot encode a surrogate: the capture must have replaced it already."""
    with pytest.raises(ValidationError):
        capture(text="a\ud800b", original_bytes=5, truncated=False)


def test_a_sha256_with_a_trailing_newline_is_rejected():
    """A `$` can match before a final newline: 64 hex characters plus a newline must fail."""
    with pytest.raises(ValidationError):
        capture(sha256="a" * 64 + "\n")
