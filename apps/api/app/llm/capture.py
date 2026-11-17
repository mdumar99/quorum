"""Cap the raw model output for storage (ADR-0003, Decision 10; issue #48, decision 3).

The stored text is the output with two length-preserving replacements, cut at a character
boundary to at most 16,384 bytes. The byte count and the SHA-256 are of the ORIGINAL output
(encoded with `surrogatepass`, so a lone surrogate counts as the 3 bytes it would take):

- each NUL becomes U+001A, one byte for one byte (Postgres text cannot hold U+0000);
- each lone surrogate becomes U+FFFD, 3 bytes for 3 bytes (UTF-8 cannot encode a surrogate).

So the stored text is never longer in bytes than the original, and the schema's
`raw_output_bytes >= octet_length(raw_output)` holds. Nothing is ever rejected.
"""

import hashlib
import re

from app.llm.types import MAX_RAW_OUTPUT_BYTES, RawOutputCapture

NUL_REPLACEMENT = "\x1a"
SURROGATE_REPLACEMENT = "\ufffd"

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

__all__ = [
    "MAX_RAW_OUTPUT_BYTES",
    "NUL_REPLACEMENT",
    "SURROGATE_REPLACEMENT",
    "capture_raw_output",
]


def capture_raw_output(text: str) -> RawOutputCapture:
    original = text.encode("utf-8", "surrogatepass")
    stored = _LONE_SURROGATE.sub(SURROGATE_REPLACEMENT, text.replace("\x00", NUL_REPLACEMENT))
    encoded = stored.encode("utf-8")
    if len(encoded) > MAX_RAW_OUTPUT_BYTES:
        # `stored` is valid UTF-8, so the cut can only leave a partial character at the end, and
        # "ignore" drops exactly that.
        stored = encoded[:MAX_RAW_OUTPUT_BYTES].decode("utf-8", "ignore")
        encoded = stored.encode("utf-8")
    return RawOutputCapture(
        text=stored,
        original_bytes=len(original),
        sha256=hashlib.sha256(original).hexdigest(),
        truncated=len(original) > len(encoded),
    )
