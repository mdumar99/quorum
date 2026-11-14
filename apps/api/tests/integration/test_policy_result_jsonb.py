"""A real PolicyResult is stored in `decisions` as quorum_app and read back equal (#46: AC8).

`decisions.rule_results` is `jsonb NOT NULL` with no shape CHECK (#44), so this shows that the
array the policy produces is accepted by the real column and by the decisions CHECKs, and comes
back unchanged. Runs in a transaction that is rolled back.
"""

from datetime import UTC, date, datetime

import pytest
from psycopg.types.json import Jsonb
from schema_helpers import insert_decision, insert_return

from app.policy import PolicyCase, evaluate, load_current_policy

pytestmark = pytest.mark.integration

AS_OF = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("case", "outcome"),
    [
        (
            {"category": "shoes", "amount_minor": 1999, "delivered_on": date(2026, 11, 1)},
            "approve",
        ),
        (
            {"category": "digital", "amount_minor": 1999, "delivered_on": date(2026, 9, 1)},
            "deny",
        ),
    ],
    ids=["all-pass", "r1-and-r2-fail"],
)
def test_result_rule_results_are_stored_in_decisions_as_jsonb_and_read_back_equal(
    app_conn, case, outcome
):
    result = evaluate(load_current_policy(), PolicyCase(currency="USD", **case), AS_OF)
    return_id = insert_return(app_conn, policy_version=result.policy_version)
    insert_decision(
        app_conn,
        return_id,
        policy_version=result.policy_version,
        outcome=outcome,
        allowed_outcomes=list(result.allowed_outcomes),
        rule_results=Jsonb(result.rule_results_json()),
    )
    stored_rule_results, stored_allowed, stored_version = app_conn.execute(
        "SELECT rule_results, allowed_outcomes, policy_version FROM public.decisions"
        " WHERE return_id = %s",
        (return_id,),
    ).fetchone()
    assert stored_rule_results == result.rule_results_json()
    assert stored_allowed == list(result.allowed_outcomes)
    assert stored_version == result.policy_version
    count = app_conn.execute(
        "SELECT jsonb_array_length(rule_results) FROM public.decisions WHERE return_id = %s",
        (return_id,),
    ).fetchone()[0]
    assert count == 3
