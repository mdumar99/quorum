"""Shared helpers for the LLM tests: the schema facts are read from db/schema.sql, never retyped."""

import re
from pathlib import Path

# tests/llm/llm_helpers.py -> llm -> tests -> api -> apps -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA = REPO_ROOT / "db" / "schema.sql"

# The ten LLM columns of audit_log and decisions (ADR-0003, Decision 10; migration 0002).
LLM_COLUMNS = (
    "provider",
    "model_id",
    "prompt_version",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "raw_output",
    "raw_output_bytes",
    "raw_output_sha256",
    "raw_output_truncated",
)
# The four errors a provider can cause. `capacity` and `abandoned` are in the schema list but
# are raised by the pipeline (P1-12, P1-13), not by the client.
PROVIDER_ERROR_TYPES = ("llm_timeout", "rate_limited", "provider_unavailable", "invalid_llm_output")
TRANSIENT = ("llm_timeout", "rate_limited", "provider_unavailable")


def schema_text() -> str:
    assert SCHEMA.is_file(), "db/schema.sql does not exist"
    return SCHEMA.read_text()


def schema_error_types() -> set[str]:
    """The values returns_error_type_ck allows."""
    line = next(
        (ln for ln in schema_text().splitlines() if "returns_error_type_ck CHECK" in ln), None
    )
    assert line is not None, "returns_error_type_ck not found in db/schema.sql"
    return set(re.findall(r"'([a-z_]+)'::text", line))


def table_columns(table: str) -> set[str]:
    """Column names of public.<table>, read from its CREATE TABLE block."""
    match = re.search(rf"CREATE TABLE public\.{table} \((.*?)\n\);", schema_text(), re.S)
    assert match is not None, f"CREATE TABLE public.{table} not found in db/schema.sql"
    names = set()
    for line in match.group(1).splitlines():
        m = re.match(r"    ([a-z0-9_]+) ", line)
        if m and m.group(1) != "CONSTRAINT":
            names.add(m.group(1))
    return names
