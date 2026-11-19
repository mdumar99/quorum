"""The payload hash (issue #54, decision 2; ADR-0004, Decision 4).

It is the SHA-256 of the canonical JSON of the seven validated fields: sorted keys, the separators
"," and ":", ASCII only, the date in ISO form, the amount an integer, never the idempotency key. No
Unicode normalisation and no trimming: the stored text is verbatim, so payloads that store
differently hash differently. `independent()` below is the canonical form written out a second time,
so the code is compared with the rule and not with itself.
"""

import hashlib
import inspect
import json
import re
from datetime import date

import pytest
from pydantic import ValidationError
from repo_unit_helpers import base_fields, raw_fields

from app.db.models import ReturnFields
from app.db.payload import payload_hash

# sha256 of {"amount_minor":1999,"category":"shoes","currency":"USD","delivered_on":"2026-11-01",
# "order_id":"order-1001","order_line_id":"line-1","reason":"does not fit"}. A change of this value
# changes what is stored in returns.payload_hash, so it needs a ticket of its own (old keys stop
# matching; they expire in 24 hours).
GOLDEN = "e525779e98849f067d99411dd5d2e3e784dfda7aadc578faa3a8513359cfd1fb"

SEVEN_FIELDS = {
    "order_id",
    "order_line_id",
    "category",
    "amount_minor",
    "currency",
    "delivered_on",
    "reason",
}


def independent(values: dict) -> str:
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_golden_value_is_pinned():
    assert payload_hash(base_fields()) == GOLDEN


def test_it_equals_the_canonical_form_written_out_independently():
    assert payload_hash(base_fields()) == independent(raw_fields())


def test_it_is_64_lowercase_hex_characters():
    assert re.fullmatch(r"[0-9a-f]{64}", payload_hash(base_fields()))


def test_the_order_of_the_keys_of_the_source_does_not_matter():
    forward = raw_fields()
    backward = dict(reversed(list(forward.items())))
    assert list(forward) != list(backward)
    assert payload_hash(ReturnFields(**forward)) == payload_hash(ReturnFields(**backward))


def test_the_whitespace_and_key_order_of_the_json_source_do_not_matter():
    compact = json.dumps(raw_fields(), separators=(",", ":"))
    pretty = json.dumps(dict(reversed(list(raw_fields().items()))), indent=4)
    assert compact != pretty
    assert payload_hash(ReturnFields.model_validate_json(compact)) == payload_hash(
        ReturnFields.model_validate_json(pretty)
    )
    assert payload_hash(ReturnFields.model_validate_json(compact)) == GOLDEN


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("order_id", "order-1002"),
        ("order_line_id", "line-2"),
        ("category", "books"),
        ("amount_minor", 2000),
        ("currency", "EUR"),
        ("delivered_on", "2026-11-02"),
        ("reason", "does not fit."),
    ],
    ids=[
        "order_id",
        "order_line_id",
        "category",
        "amount_minor",
        "currency",
        "delivered_on",
        "reason",
    ],
)
def test_each_of_the_seven_fields_changes_the_hash(field, other):
    assert payload_hash(base_fields(**{field: other})) != GOLDEN


def test_the_reason_is_hashed_verbatim_with_no_trimming():
    assert payload_hash(base_fields(reason=" does not fit")) != GOLDEN
    assert payload_hash(base_fields(reason="does not fit\n")) != GOLDEN


def test_non_ascii_text_is_stable_and_matches_the_ascii_escaped_canonical_form():
    reason = "café 日本語 ☃ 😀"
    first = payload_hash(base_fields(reason=reason))
    assert first == payload_hash(base_fields(reason=reason))
    assert first == independent(raw_fields(reason=reason))
    assert first != GOLDEN


def test_there_is_no_unicode_normalisation():
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    assert composed != decomposed
    assert payload_hash(base_fields(reason=composed)) != payload_hash(
        base_fields(reason=decomposed)
    )


def test_the_date_is_hashed_in_iso_form():
    as_object = ReturnFields(**{**raw_fields(), "delivered_on": date(2026, 11, 1)})
    as_text = ReturnFields(**raw_fields(delivered_on="2026-11-01"))
    assert payload_hash(as_object) == payload_hash(as_text) == GOLDEN


def test_the_amount_is_hashed_as_an_integer():
    # The golden value was built with 1999, not "1999": a string amount would give another hash.
    assert independent(raw_fields(amount_minor="1999")) != GOLDEN
    assert payload_hash(base_fields(amount_minor=1999)) == GOLDEN


def test_the_function_takes_only_the_seven_fields_and_never_a_key():
    parameters = inspect.signature(payload_hash).parameters
    assert list(parameters) == ["fields"]
    assert parameters["fields"].annotation is ReturnFields
    assert set(ReturnFields.model_fields) == SEVEN_FIELDS
    with pytest.raises(ValidationError):
        ReturnFields(**raw_fields(), idempotency_key="key-1")
