"""The `audit_log` table (#44, criterion S6).

Sources: ADR-0003 D10, ADR-0004 D4, D5, D6 and ADR-0002 D3 (identity column, no sequence grant), as
the approved schema proposal on #44 reads them. Every test runs as quorum_app through app_conn, in
a transaction that is rolled back.
"""

from uuid import uuid4

import pytest
from psycopg import errors
from schema_helpers import (
    RAW_OUTPUT_ACCEPTED,
    RAW_OUTPUT_REJECTED,
    TOKEN_COLUMNS,
    insert_audit,
    insert_return,
    llm_fields,
    one,
    rejected,
    require_relation,
)

pytestmark = pytest.mark.integration

PROVIDER_GROUP = ("provider", "model_id", "prompt_version")
RAW_GROUP = ("raw_output", "raw_output_bytes", "raw_output_sha256", "raw_output_truncated")


def test_audit_log_id_is_generated_always_identity(app_conn):
    require_relation(app_conn, "audit_log")
    identity = one(
        app_conn,
        "SELECT attidentity FROM pg_attribute"
        " WHERE attrelid = 'public.audit_log'::regclass AND attname = 'id'",
    )
    assert identity == "a"  # 'a' is GENERATED ALWAYS, 'd' would be BY DEFAULT


def test_audit_row_requires_return_attempt_and_event_type(app_conn):
    return_id = insert_return(app_conn)
    for column in ("return_id", "attempt", "event_type"):
        values = {"return_id": return_id, "attempt": 1, "event_type": "attempt_started"}
        values[column] = None
        with rejected(app_conn, errors.NotNullViolation, column=column):
            insert_audit(app_conn, values.pop("return_id"), **values)
    with rejected(app_conn, errors.CheckViolation, "audit_log_attempt_ck"):
        insert_audit(app_conn, return_id, attempt=0)
    with rejected(app_conn, errors.CheckViolation, "audit_log_event_type_ck"):
        insert_audit(app_conn, return_id, event_type="")
    with rejected(app_conn, errors.ForeignKeyViolation, "audit_log_return_fk"):
        insert_audit(app_conn, uuid4())  # no such return


def test_audit_llm_call_columns_are_all_or_none(app_conn):
    return_id = insert_return(app_conn)
    fields = llm_fields()
    # Accepted: no LLM call; a call that timed out (provider group only); a full call.
    insert_audit(app_conn, return_id)
    insert_audit(app_conn, return_id, **{c: fields[c] for c in PROVIDER_GROUP}, latency_ms=2000)
    insert_audit(app_conn, return_id, **fields)
    # Rejected: part of the provider group, or part of the raw-output group.
    partial_provider = [PROVIDER_GROUP[:1], PROVIDER_GROUP[:2], PROVIDER_GROUP[1:]]
    for group in partial_provider:
        with rejected(app_conn, errors.CheckViolation, "audit_log_llm_call_ck"):
            insert_audit(app_conn, return_id, **{c: fields[c] for c in group})
    partial_raw = [RAW_GROUP[:1], RAW_GROUP[:2], RAW_GROUP[1:]]
    for group in partial_raw:
        with rejected(app_conn, errors.CheckViolation, "audit_log_raw_output_ck"):
            insert_audit(
                app_conn,
                return_id,
                **{c: fields[c] for c in PROVIDER_GROUP},
                **{c: fields[c] for c in group},
            )


@pytest.mark.parametrize("column", ["latency_ms", *TOKEN_COLUMNS])
def test_audit_latency_and_token_counts_cannot_be_negative(app_conn, column):
    # ADDED beyond the section 8 list: the ">= 0" of these columns, read as part of
    # audit_log_llm_call_ck (the proposal names no separate constraint for it).
    return_id = insert_return(app_conn)
    fields = {c: llm_fields()[c] for c in PROVIDER_GROUP}
    with rejected(app_conn, errors.CheckViolation, "audit_log_llm_call_ck"):
        insert_audit(app_conn, return_id, **fields, **{column: -1})


def test_conflicting_return_id_must_exist(app_conn):
    return_id = insert_return(app_conn)
    other = insert_return(app_conn)
    insert_audit(app_conn, return_id, conflicting_return_id=other)
    with rejected(app_conn, errors.ForeignKeyViolation, "audit_log_conflicting_return_fk"):
        insert_audit(app_conn, return_id, conflicting_return_id=uuid4())


@pytest.mark.parametrize("values", RAW_OUTPUT_ACCEPTED.values(), ids=RAW_OUTPUT_ACCEPTED.keys())
def test_audit_raw_output_cap_and_consistency_accepts(app_conn, values):
    # The same cases as for `decisions`, with the provider group set (llm_fields sets all ten).
    insert_audit(app_conn, insert_return(app_conn), **llm_fields(**values))


@pytest.mark.parametrize("values", RAW_OUTPUT_REJECTED.values(), ids=RAW_OUTPUT_REJECTED.keys())
def test_audit_raw_output_cap_and_consistency_rejects(app_conn, values):
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.CheckViolation, "audit_log_raw_output_ck"):
        insert_audit(app_conn, return_id, **llm_fields(**values))
