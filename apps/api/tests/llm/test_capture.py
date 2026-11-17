"""Capping the raw output at 16,384 bytes (ADR-0003, Decision 10; #48, decision 3).

Rules under test: cut at a character boundary; the byte count and the SHA-256 are of the ORIGINAL
output; each NUL becomes the one-byte U+001A and each lone surrogate becomes U+FFFD (3 bytes, the
same length as the surrogate under `surrogatepass`), so the stored text is never longer in bytes
than the original and `raw_output_bytes >= octet_length(raw_output)` holds in the schema.
"""

import hashlib
import re

import pytest

from app.llm.capture import (
    MAX_RAW_OUTPUT_BYTES,
    NUL_REPLACEMENT,
    SURROGATE_REPLACEMENT,
    capture_raw_output,
)

LIMIT = 16_384


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_the_constants_are_the_ones_in_the_adr():
    assert MAX_RAW_OUTPUT_BYTES == LIMIT
    assert NUL_REPLACEMENT == "\x1a"
    assert nbytes(NUL_REPLACEMENT) == 1  # one byte, so the schema check keeps holding
    assert SURROGATE_REPLACEMENT == "\ufffd"
    assert nbytes(SURROGATE_REPLACEMENT) == 3


def test_short_output_is_stored_verbatim():
    cap = capture_raw_output('{"outcome": "deny"}')
    assert cap.text == '{"outcome": "deny"}'
    assert cap.original_bytes == 19
    assert cap.sha256 == sha(b'{"outcome": "deny"}')
    assert cap.truncated is False


def test_the_text_is_not_stripped_or_normalised():
    text = "  line one\n\tline two  \r\n" + "e\u0301" + "\u00e9"  # decomposed and composed e-acute
    assert capture_raw_output(text).text == text


def test_empty_output_is_still_a_capture():
    cap = capture_raw_output("")
    assert cap.text == ""
    assert cap.original_bytes == 0
    assert cap.sha256 == sha(b"")
    assert cap.truncated is False


def test_output_of_exactly_the_limit_is_not_truncated():
    cap = capture_raw_output("a" * LIMIT)
    assert cap.text == "a" * LIMIT
    assert cap.original_bytes == LIMIT
    assert cap.truncated is False


def test_one_byte_over_the_limit_is_truncated():
    cap = capture_raw_output("a" * (LIMIT + 1))
    assert cap.text == "a" * LIMIT
    assert cap.original_bytes == LIMIT + 1
    assert cap.truncated is True


def test_multibyte_output_that_fits_exactly_is_not_truncated():
    cap = capture_raw_output("\u00e9" * 8192)
    assert cap.text == "\u00e9" * 8192
    assert cap.original_bytes == LIMIT
    assert cap.truncated is False


@pytest.mark.parametrize("char", ["\u00e9", "€", "😀"], ids=["2-byte", "3-byte", "4-byte"])
@pytest.mark.parametrize("prefix", range(4))
def test_the_cut_is_at_a_character_boundary_and_as_late_as_possible(char, prefix):
    size = nbytes(char)
    text = "a" * prefix + char * (LIMIT // size + 2)
    cap = capture_raw_output(text)
    stored = cap.text.encode("utf-8")  # valid UTF-8: this raises if a character was split
    assert len(stored) <= LIMIT
    assert LIMIT - len(stored) < size  # nothing that would still have fitted was dropped
    assert text.startswith(cap.text)
    assert cap.truncated is True
    assert cap.original_bytes == nbytes(text)


def test_count_and_hash_are_of_the_full_original_when_cut():
    text = "\u00e9" * 20_000
    cap = capture_raw_output(text)
    assert cap.original_bytes == 40_000
    assert cap.sha256 == sha(text.encode("utf-8"))
    assert cap.sha256 != sha(cap.text.encode("utf-8"))


def test_the_hash_is_64_lowercase_hex():
    assert re.fullmatch(r"[0-9a-f]{64}", capture_raw_output("x").sha256)


@pytest.mark.parametrize("text", ["\x00", "a\x00b", "\x00\x00\x00", '{"a": "b\x00"}'])
def test_nul_becomes_one_byte_and_the_original_is_what_is_counted_and_hashed(text):
    cap = capture_raw_output(text)
    assert "\x00" not in cap.text
    assert cap.text == text.replace("\x00", "\x1a")
    assert cap.original_bytes == nbytes(text)
    assert nbytes(cap.text) == nbytes(text)  # not one byte longer or shorter
    assert cap.sha256 == sha(text.encode("utf-8"))  # of the original, with the NULs
    assert cap.truncated is False


def test_nul_is_replaced_before_the_cut():
    text = "\x00" * (LIMIT + 1)
    cap = capture_raw_output(text)
    assert cap.text == "\x1a" * LIMIT
    assert cap.original_bytes == LIMIT + 1
    assert cap.sha256 == sha(text.encode("utf-8"))
    assert cap.truncated is True


def test_a_lone_surrogate_becomes_one_replacement_character_of_the_same_size():
    text = "a\ud800b\udfffc"
    cap = capture_raw_output(text)
    assert cap.text == "a\ufffdb\ufffdc"
    cap.text.encode("utf-8")  # stored text is valid UTF-8
    assert cap.original_bytes == 1 + 3 + 1 + 3 + 1  # surrogatepass: 3 bytes each
    assert nbytes(cap.text) == cap.original_bytes
    assert cap.sha256 == sha(text.encode("utf-8", "surrogatepass"))
    assert cap.truncated is False


def test_a_surrogate_next_to_the_cut_is_handled():
    text = "a" * (LIMIT - 1) + "\ud800" * 2
    cap = capture_raw_output(text)
    assert cap.text == "a" * (LIMIT - 1)  # U+FFFD would end at LIMIT + 2: dropped whole
    assert cap.original_bytes == LIMIT - 1 + 6
    assert cap.truncated is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain",
        "\u00e9" * 9000,
        "€" * 6000,
        "😀" * 4100,
        "a" * 16_383 + "€",
        "\x00" * 20_000,
        "x\ud800" * 9000,
        "mixed \u00e9€😀\x00\ud800 " * 2000,
    ],
    ids=["empty", "ascii", "2-byte", "3-byte", "4-byte", "split-3-byte", "nul", "surr", "mixed"],
)
def test_every_capture_satisfies_the_schema_checks(text):
    """The CHECKs of audit_log and decisions, applied to the capture (migration 0002)."""
    cap = capture_raw_output(text)
    stored = nbytes(cap.text)
    assert stored <= LIMIT
    assert cap.original_bytes >= stored
    assert cap.truncated == (cap.original_bytes > stored)
    assert re.fullmatch(r"[0-9a-f]{64}", cap.sha256)
    assert "\x00" not in cap.text
