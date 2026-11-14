"""Row builders and a "must fail" helper for the schema tests of #44 (ADR-0002, ADR-0003, ADR-0004).

These are not tests. Every test that uses them runs as the real quorum_app through the app_conn
fixture, inside a transaction that is rolled back at the end, so nothing is left behind
(quorum_app cannot delete anything).
"""

import hashlib
from contextlib import contextmanager
from datetime import date
from uuid import UUID, uuid4

import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

# Pass OMIT as a column's value to leave that column out of the INSERT, to see its default.
OMIT = object()

POLICY_VERSION = "policy-v1:0123abcd"
PROMPT_VERSION = "prompt-v1:4567cdef"

TOKEN_COLUMNS = ("prompt_tokens", "completion_tokens")
# The ten LLM columns of `decisions` (ADR-0003 D10); `audit_log` has the same ten.
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


def unique(prefix: str) -> str:
    """A value that is unique per call, so rows never collide by accident."""
    return f"{prefix}-{uuid4().hex[:12]}"


def one(conn: Connection, query: str, params: tuple = ()):
    return conn.execute(query, params).fetchone()[0]


def require_relation(conn: Connection, name: str) -> None:
    """Fail with a clear message when a table of the schema does not exist."""
    found = one(conn, "SELECT to_regclass(%s)", (f"public.{name}",))
    assert found is not None, f"table public.{name} does not exist"


def _insert(conn: Connection, table: str, values: dict, returning: str):
    values = {column: value for column, value in values.items() if value is not OMIT}
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        sql.Identifier("public", table),
        sql.SQL(", ").join(sql.Identifier(column) for column in values),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
        sql.Identifier(returning),
    )
    return conn.execute(query, list(values.values())).fetchone()[0]


def insert_return(conn: Connection, **overrides) -> UUID:
    """Insert a valid `processing` return; every column can be overridden or left out (OMIT)."""
    values = {
        "order_id": unique("order"),
        "order_line_id": unique("line"),
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "EUR",
        "delivered_on": date(2026, 11, 1),
        "reason": "does not fit",
        "status": "processing",
        "policy_version": POLICY_VERSION,
        "idempotency_key": unique("key"),
        "payload_hash": "0" * 64,
    }
    values.update(overrides)
    return _insert(conn, "returns", values, "id")


def llm_fields(raw_output: str = '{"outcome": "deny"}', **overrides) -> dict:
    """The ten LLM columns with consistent values (the raw output, its length and its SHA-256)."""
    raw = raw_output.encode()
    fields = {
        "provider": "groq",
        "model_id": "model-1",
        "prompt_version": PROMPT_VERSION,
        "latency_ms": 120,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "raw_output": raw_output,
        "raw_output_bytes": len(raw),
        "raw_output_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_output_truncated": False,
    }
    fields.update(overrides)
    return fields


# Raw-output cases shared by `decisions` and `audit_log`: both have the same cap and consistency
# rule (ADR-0003 D10). Each value is passed to llm_fields(**values).
RAW_OUTPUT_ACCEPTED = {
    "16384-bytes": {"raw_output": "a" * 16384, "raw_output_bytes": 16384},
    "multibyte-16384-bytes": {"raw_output": "é" * 8192, "raw_output_bytes": 16384},
    "truncated": {
        "raw_output": "a" * 16384,
        "raw_output_bytes": 20000,
        "raw_output_truncated": True,
    },
}
RAW_OUTPUT_REJECTED = {
    "16385-bytes": {"raw_output": "a" * 16385, "raw_output_bytes": 16385},
    "multibyte-16386-bytes": {"raw_output": "é" * 8193, "raw_output_bytes": 16386},
    "truncated-flag-false-but-longer": {
        "raw_output": "a" * 16384,
        "raw_output_bytes": 20000,
        "raw_output_truncated": False,
    },
    "truncated-flag-true-but-same-length": {
        "raw_output": "a" * 100,
        "raw_output_bytes": 100,
        "raw_output_truncated": True,
    },
    "original-length-below-the-stored-length": {"raw_output": "a" * 100, "raw_output_bytes": 99},
    "sha256-too-short": {"raw_output": "ok", "raw_output_bytes": 2, "raw_output_sha256": "ab" * 31},
    "sha256-uppercase": {"raw_output": "ok", "raw_output_bytes": 2, "raw_output_sha256": "A" * 64},
    "sha256-not-hex": {"raw_output": "ok", "raw_output_bytes": 2, "raw_output_sha256": "z" * 64},
}


def insert_decision(conn: Connection, return_id: UUID, **overrides) -> UUID:
    """Insert a valid rules decision (a hard deny); LLM columns are left null."""
    values = {
        "return_id": return_id,
        "policy_version": POLICY_VERSION,
        "outcome": "deny",
        "decided_by": "rules",
        "rule_results": Jsonb([]),
        "allowed_outcomes": ["deny"],
    }
    values.update(overrides)
    return _insert(conn, "decisions", values, "return_id")


def insert_llm_decision(conn: Connection, return_id: UUID, **overrides) -> UUID:
    """Insert a valid decision made by the model, with all the LLM columns set."""
    values = {
        "outcome": "approve",
        "decided_by": "llm",
        "allowed_outcomes": ["approve", "deny", "escalate"],
        **llm_fields(),
    }
    values.update(overrides)
    return insert_decision(conn, return_id, **values)


def insert_audit(conn: Connection, return_id: UUID | None, **overrides) -> int:
    """Insert a valid audit row for an attempt; returns its generated id."""
    values = {"return_id": return_id, "attempt": 1, "event_type": "attempt_started"}
    values.update(overrides)
    return _insert(conn, "audit_log", values, "id")


@contextmanager
def rejected(
    conn: Connection, error: type[Exception], *constraints: str, column: str | None = None
):
    """Run a statement that must fail, inside a savepoint so the transaction survives.

    constraints: the violated CHECK, unique index or foreign key must be one of these names.
    column: for a NOT NULL violation, the column that was null.
    """
    with pytest.raises(error) as caught:
        with conn.transaction():
            yield
    if constraints:
        assert caught.value.diag.constraint_name in constraints
    if column is not None:
        assert caught.value.diag.column_name == column
