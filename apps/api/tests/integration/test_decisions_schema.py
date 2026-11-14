"""The `decisions` table (#44, criterion S5).

Sources: ADR-0003 D3 and D10, ADR-0004 D4, as the approved schema proposal on #44 reads them.
`decisions` is keyed by return_id (one decision per return in Phase 1). Every test runs as
quorum_app through app_conn, in a transaction that is rolled back.
"""

from uuid import uuid4

import pytest
from psycopg import errors
from schema_helpers import (
    LLM_COLUMNS,
    RAW_OUTPUT_ACCEPTED,
    RAW_OUTPUT_REJECTED,
    TOKEN_COLUMNS,
    insert_decision,
    insert_llm_decision,
    insert_return,
    llm_fields,
    rejected,
)

pytestmark = pytest.mark.integration


def test_decision_roundtrip_for_a_rules_deny_and_an_llm_decision(app_conn):
    rules_return = insert_return(app_conn)
    insert_decision(app_conn, rules_return)
    llm_return = insert_return(app_conn)
    insert_llm_decision(app_conn, llm_return)
    columns = "outcome, decided_by, allowed_outcomes, provider, raw_output_truncated"
    query = f"SELECT {columns} FROM public.decisions WHERE return_id = %s"
    assert app_conn.execute(query, (rules_return,)).fetchone() == (
        "deny",
        "rules",
        ["deny"],
        None,
        None,
    )
    assert app_conn.execute(query, (llm_return,)).fetchone() == (
        "approve",
        "llm",
        ["approve", "deny", "escalate"],
        "groq",
        False,
    )


def test_decision_policy_version_must_match_the_return(app_conn):
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.ForeignKeyViolation, "decisions_return_policy_fk"):
        insert_decision(app_conn, return_id, policy_version="another-version:ffff")
    with rejected(app_conn, errors.ForeignKeyViolation, "decisions_return_policy_fk"):
        insert_decision(app_conn, uuid4())  # no such return


@pytest.mark.parametrize("direction", ["rules-with-the-column", "llm-without-the-column"])
@pytest.mark.parametrize("column", LLM_COLUMNS)
def test_llm_fields_only_when_decided_by_llm(app_conn, column, direction):
    return_id = insert_return(app_conn)
    if direction == "rules-with-the-column":
        # A rules decision must have all ten LLM columns null, so any one of them set is rejected.
        with rejected(app_conn, errors.CheckViolation, "decisions_llm_fields_ck"):
            insert_decision(app_conn, return_id, **{column: llm_fields()[column]})
    elif column in TOKEN_COLUMNS:
        # The two token counts may stay null, even for an llm decision.
        insert_llm_decision(app_conn, return_id, **{column: None})
    else:
        with rejected(app_conn, errors.CheckViolation, "decisions_llm_fields_ck"):
            insert_llm_decision(app_conn, return_id, **{column: None})


@pytest.mark.parametrize("column", ["latency_ms", *TOKEN_COLUMNS])
def test_latency_and_token_counts_cannot_be_negative(app_conn, column):
    # ADDED beyond the section 8 list: the ">= 0" of these columns, read as part of
    # decisions_llm_fields_ck (the proposal names no separate constraint for it).
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.CheckViolation, "decisions_llm_fields_ck"):
        insert_llm_decision(app_conn, return_id, **{column: -1})


@pytest.mark.parametrize("values", RAW_OUTPUT_ACCEPTED.values(), ids=RAW_OUTPUT_ACCEPTED.keys())
def test_raw_output_cap_and_consistency_accepts(app_conn, values):
    insert_llm_decision(app_conn, insert_return(app_conn), **llm_fields(**values))


@pytest.mark.parametrize("values", RAW_OUTPUT_REJECTED.values(), ids=RAW_OUTPUT_REJECTED.keys())
def test_raw_output_cap_and_consistency_rejects(app_conn, values):
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.CheckViolation, "decisions_raw_output_ck"):
        insert_llm_decision(app_conn, return_id, **llm_fields(**values))


def test_one_decision_per_return(app_conn):
    return_id = insert_return(app_conn)
    insert_decision(app_conn, return_id)
    with rejected(app_conn, errors.UniqueViolation, "decisions_pkey"):
        insert_decision(app_conn, return_id)


# decisions_allowed_outcomes_ck judges the list alone (non-empty, every element known);
# decisions_outcome_ck judges the outcome (known, and one of the list). Postgres reports the first
# failing CHECK in name order, and allowed_outcomes_ck sorts before outcome_ck.
def test_outcome_must_be_in_allowed_outcomes(app_conn):
    insert_decision(
        app_conn, insert_return(app_conn), outcome="escalate", allowed_outcomes=["deny", "escalate"]
    )
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.CheckViolation, "decisions_outcome_ck"):
        # a valid list, but the outcome is not among the allowed ones
        insert_decision(
            app_conn, return_id, outcome="approve", allowed_outcomes=["deny", "escalate"]
        )


@pytest.mark.parametrize(
    "allowed",
    [pytest.param([], id="empty-list"), pytest.param(["deny", "bogus"], id="unknown-element")],
)
def test_allowed_outcomes_must_be_non_empty_and_known(app_conn, allowed):
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.CheckViolation, "decisions_allowed_outcomes_ck"):
        insert_decision(app_conn, return_id, outcome="deny", allowed_outcomes=allowed)


def test_decision_rejects_unknown_outcome_and_decided_by(app_conn):
    # ADDED beyond the section 8 list: gives decisions_outcome_ck and decisions_decided_by_ck a
    # rejecting test, which test_every_check_constraint_is_exercised requires. The allowed list is
    # valid, so the outcome is the only thing wrong in the first case.
    with rejected(app_conn, errors.CheckViolation, "decisions_outcome_ck"):
        insert_decision(
            app_conn, insert_return(app_conn), outcome="maybe", allowed_outcomes=["deny"]
        )
    with rejected(app_conn, errors.CheckViolation, "decisions_decided_by_ck"):
        insert_decision(app_conn, insert_return(app_conn), decided_by="human")


def test_rule_results_must_be_present(app_conn):
    # Only NOT NULL: the shape of rule_results belongs to the policy ticket (P1-5).
    return_id = insert_return(app_conn)
    with rejected(app_conn, errors.NotNullViolation, column="rule_results"):
        insert_decision(app_conn, return_id, rule_results=None)
    insert_decision(app_conn, return_id)  # any jsonb is accepted: the helper passes an empty array
