"""Capped LLM output is stored as quorum_app in audit_log and decisions (#48: AC9).

The capture is built by the real `capture_raw_output`, wrapped in a call record, and written with
`record.columns()`: the schema CHECKs on the raw output (at most 16,384 bytes, the original length
at least the stored length, the truncated flag equal to "original is longer", a 64-hex SHA-256)
accept every case, and what comes back is what was stored. Runs in a transaction that is rolled
back.
"""

import psycopg
import pytest
from schema_helpers import insert_audit, insert_llm_decision, insert_return, llm_fields

from app.llm import LLMCallRecord, capture_raw_output

pytestmark = pytest.mark.integration

CASES = {
    "normal": '{"outcome": "deny", "rationale": "outside the window"}',
    "empty": "",
    "exactly-16384": "a" * 16_384,
    "truncated": "a" * 20_000,
    "multibyte-cut": "\u00e9" * 9000,
    "four-byte-cut": "😀" * 4100,
    "nul": "a\x00b",
    "nul-before-the-cut": "\x00" * 16_385,
    "lone-surrogate": "a\ud800b",
    "mixed": "\u00e9€😀\x00\ud800 " * 3000,
}


def record_for(text: str) -> LLMCallRecord:
    return LLMCallRecord(
        provider="fake",
        model_id="fake-v1",
        prompt_version="pv-1",
        latency_ms=3,
        prompt_tokens=None,
        completion_tokens=None,
        raw_output=capture_raw_output(text),
    )


def read_back(conn, table: str, key_column: str, key) -> tuple:
    return conn.execute(
        "SELECT raw_output, raw_output_bytes, raw_output_sha256, raw_output_truncated,"
        f" octet_length(raw_output) FROM public.{table} WHERE {key_column} = %s",
        (key,),
    ).fetchone()


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_a_capture_is_accepted_by_audit_log_and_comes_back_unchanged(app_conn, text):
    record = record_for(text)
    return_id = insert_return(app_conn)
    audit_id = insert_audit(app_conn, return_id, event_type="llm_call", **record.columns())
    raw, original, sha, truncated, stored_bytes = read_back(app_conn, "audit_log", "id", audit_id)
    cap = record.raw_output
    assert (raw, original, sha, truncated) == (
        cap.text,
        cap.original_bytes,
        cap.sha256,
        cap.truncated,
    )
    assert original >= stored_bytes
    assert truncated == (original > stored_bytes)


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_a_capture_is_accepted_by_decisions_and_comes_back_unchanged(app_conn, text):
    record = record_for(text)
    return_id = insert_return(app_conn)
    insert_llm_decision(app_conn, return_id, **record.columns())
    raw, original, sha, truncated, _ = read_back(app_conn, "decisions", "return_id", return_id)
    cap = record.raw_output
    assert (raw, original, sha, truncated) == (
        cap.text,
        cap.original_bytes,
        cap.sha256,
        cap.truncated,
    )


def test_a_failed_calls_record_without_output_is_a_valid_audit_row(app_conn):
    record = LLMCallRecord(provider="fake", model_id="fake-v1", prompt_version="pv-1", latency_ms=0)
    return_id = insert_return(app_conn)
    audit_id = insert_audit(app_conn, return_id, event_type="llm_call", **record.columns())
    assert read_back(app_conn, "audit_log", "id", audit_id)[:4] == (None, None, None, None)


def test_the_uncapped_nul_is_what_postgres_cannot_store(app_conn):
    """Why decision 3 exists: without the replacement the driver refuses the text."""
    return_id = insert_return(app_conn)
    with pytest.raises(psycopg.DataError):
        insert_audit(app_conn, return_id, event_type="llm_call", **llm_fields(raw_output="a\x00b"))


def test_the_columns_of_a_record_match_the_helpers_own_ten():
    assert set(record_for("x").columns()) == set(llm_fields())
