"""Criterion 2: R4 on insert and on retry, and the key conflict (issue #54; ADR-0004, Decision 6).

`ACTIVE_ORDER_LINE_INDEX` is the partial unique index that is R4; `KEY_INDEX` is the key index.
Setup is done as the owner; the functions under test run as quorum_app.

Two situations need a way to change the world BETWEEN two statements of one repository call, and
neither can be made by timing:
- the blocker that leaves the active set after the collision and before the lookup of the other
  return: `statement_hook` runs an owner UPDATE just before the lookup statement (it recognises the
  statement by `ACTIVE_RETURN_PREDICATE_SQL`);
- the blocker that is never found: the app connection runs at REPEATABLE READ, its snapshot is
  taken BEFORE the blocker is committed, so the unique-index check sees the blocker (it is not
  snapshot-based) and the lookup does not. This relies on Postgres raising a plain unique
  violation, not a serialization failure, in that case; the first run shows whether it does.
"""

import psycopg
import pytest
from psycopg import IsolationLevel
from repo_data import (
    HASH_A,
    HASH_B,
    LLM_COLUMNS,
    PV,
    R4_ARRAY,
    app_connection,
    audit_rows,
    backdate_updated,
    count_by_key,
    decision_rows,
    fields_for,
    is_returns_insert,
    is_returns_update,
    key_for,
    new_return,
    owner_row,
    run,
    statement_hook,
    with_conn,
)

from app.db.models import Inserted, KeyConflict, R4Denied, Retried
from app.db.returns import ACTIVE_RETURN_PREDICATE_SQL, get_return, insert_return, retry_failed

pytestmark = pytest.mark.integration

BLOCKERS = [
    pytest.param("processing", None, id="processing"),
    pytest.param("pending_review", "escalate", id="pending_review"),
    pytest.param("decided", "approve", id="decided-approve"),
]


async def insert(url, fields, key, payload_hash=HASH_B, generation=1, **kwargs):
    async def action(conn):
        return await insert_return(
            conn,
            fields,
            idempotency_key=key,
            payload_hash=payload_hash,
            key_generation=generation,
            policy_version=PV,
        )

    return await with_conn(url, action, **kwargs)


# ---------- R4 on insert ----------


@pytest.mark.parametrize(("status", "outcome"), BLOCKERS)
def test_a_second_key_on_the_same_order_line_is_stored_as_a_deny(
    marker, owner_conn, app_url, status, outcome
):
    blocker = new_return(owner_conn, marker, name="a", status=status, outcome=outcome)
    fields = fields_for(marker)
    result = run(insert(app_url, fields, key_for(marker, "b")))
    assert isinstance(result, R4Denied)
    stored = owner_row(owner_conn, result.row.id)
    assert (stored["status"], stored["outcome"]) == ("decided", "deny")
    assert stored["error_type"] is None
    assert stored["retry_after_seconds"] is None
    assert stored["attempt"] == 1
    assert stored["idempotency_key"] == key_for(marker, "b")
    assert stored["key_generation"] == 1
    assert stored["payload_hash"] == HASH_B
    assert stored["policy_version"] == PV
    for name, value in fields.model_dump().items():
        assert stored[name] == value, name
    assert result.row.model_dump() == stored
    assert owner_row(owner_conn, blocker)["status"] == status  # the blocker is untouched


@pytest.mark.parametrize(("status", "outcome"), BLOCKERS)
def test_the_r4_deny_has_its_decision_row(marker, owner_conn, app_url, status, outcome):
    new_return(owner_conn, marker, name="a", status=status, outcome=outcome)
    result = run(insert(app_url, fields_for(marker), key_for(marker, "b")))
    decisions = decision_rows(owner_conn, result.row.id)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["decided_by"] == "rules"
    assert decision["outcome"] == "deny"
    assert decision["rule_results"] == R4_ARRAY
    assert decision["allowed_outcomes"] == ["deny"]
    assert decision["policy_version"] == PV
    assert all(decision[column] is None for column in LLM_COLUMNS)


@pytest.mark.parametrize(("status", "outcome"), BLOCKERS)
def test_the_r4_deny_has_one_audit_row_that_names_the_blocker(
    marker, owner_conn, app_url, status, outcome
):
    blocker = new_return(owner_conn, marker, name="a", status=status, outcome=outcome)
    result = run(insert(app_url, fields_for(marker), key_for(marker, "b")))
    audits = audit_rows(owner_conn, result.row.id)
    assert len(audits) == 1
    assert audits[0]["event_type"] == "r4_denied"
    assert audits[0]["attempt"] == 1
    assert audits[0]["conflicting_return_id"] == blocker
    assert all(audits[0][column] is None for column in LLM_COLUMNS)
    # nothing was written for the blocker
    assert audit_rows(owner_conn, blocker) == []
    assert decision_rows(owner_conn, blocker) == []


def test_the_other_returns_id_is_in_no_field_of_what_the_function_returns(
    marker, owner_conn, app_url
):
    blocker = new_return(owner_conn, marker, name="a", status="processing")
    result = run(insert(app_url, fields_for(marker), key_for(marker, "b")))
    assert isinstance(result, R4Denied)
    for text in (result.model_dump_json(), repr(result), str(result), repr(result.row)):
        assert str(blocker) not in text
    assert result.row.id != blocker


def test_the_connection_survives_the_savepoint_and_the_rows_commit_together(
    marker, owner_conn, app_url
):
    new_return(owner_conn, marker, name="a", status="processing")

    async def action(conn):
        result = await insert_return(
            conn,
            fields_for(marker),
            idempotency_key=key_for(marker, "b"),
            payload_hash=HASH_B,
            key_generation=1,
            policy_version=PV,
        )
        # Without a savepoint the 23505 would leave the transaction aborted and this would fail.
        again = await get_return(conn, result.row.id)
        return result, again

    result, again = run(with_conn(app_url, action))
    assert isinstance(result, R4Denied)
    assert again == result.row
    assert len(decision_rows(owner_conn, result.row.id)) == 1
    assert len(audit_rows(owner_conn, result.row.id)) == 1


def test_the_next_generation_of_an_expired_key_under_a_blocker_is_still_r4(
    marker, owner_conn, app_url
):
    blocker = new_return(owner_conn, marker, name="a", status="processing")
    result = run(insert(app_url, fields_for(marker), key_for(marker, "a"), generation=2))
    assert isinstance(result, R4Denied)
    assert result.row.key_generation == 2
    assert audit_rows(owner_conn, result.row.id)[0]["conflicting_return_id"] == blocker


# ---------- R4 on a retry ----------


def test_a_retry_that_hits_the_index_ends_as_a_deny_with_attempt_n_plus_1(
    marker, owner_conn, app_url
):
    blocker = new_return(owner_conn, marker, name="a", status="processing")
    failed = new_return(
        owner_conn,
        marker,
        name="b",
        status="failed",
        error_type="llm_timeout",
        retry_after_seconds=30,
        attempt=2,
        payload_hash=HASH_B,
    )
    backdate_updated(owner_conn, failed, 3600)  # so that "updated_at moved forward" can be seen
    before = owner_row(owner_conn, failed)

    async def action(conn):
        return await retry_failed(conn, await get_return(conn, failed))

    result = run(with_conn(app_url, action))
    assert isinstance(result, R4Denied)
    stored = owner_row(owner_conn, failed)
    assert stored["updated_at"] > before["updated_at"]  # the deny UPDATE sets updated_at itself
    assert result.row.id == failed
    assert (stored["status"], stored["outcome"]) == ("decided", "deny")
    assert stored["error_type"] is None
    assert stored["retry_after_seconds"] is None
    assert stored["attempt"] == 3  # the retry was an attempt: n + 1
    assert stored["idempotency_key"] == key_for(marker, "b")
    assert result.row.model_dump() == stored
    decisions = decision_rows(owner_conn, failed)
    assert len(decisions) == 1
    assert decisions[0]["decided_by"] == "rules"
    assert decisions[0]["rule_results"] == R4_ARRAY
    assert decisions[0]["allowed_outcomes"] == ["deny"]
    audits = audit_rows(owner_conn, failed)
    assert [(a["event_type"], a["attempt"]) for a in audits] == [("r4_denied", 3)]
    assert audits[0]["conflicting_return_id"] == blocker
    assert owner_row(owner_conn, blocker)["status"] == "processing"


# ---------- the key conflict is never R4 ----------

KEY_CONFLICTS = [
    pytest.param("processing", None, None, "2", id="other-line-blocker-processing"),
    pytest.param("processing", None, None, "1", id="same-line-both-indexes-processing"),
    pytest.param("decided", "approve", None, "1", id="same-line-both-indexes-approved"),
    pytest.param("decided", "deny", None, "1", id="same-line-only-the-key-index-denied"),
    pytest.param("failed", None, "llm_timeout", "1", id="same-line-only-the-key-index-failed"),
]


@pytest.mark.parametrize(("status", "outcome", "error_type", "line"), KEY_CONFLICTS)
def test_the_same_key_and_generation_is_a_key_conflict_not_r4(
    marker, owner_conn, app_url, status, outcome, error_type, line
):
    # The first row sits on order line 1. The new insert uses the same key and generation, on the
    # same line (where both unique indexes may be violated, and Postgres reports one of them
    # first: this must come out as a key conflict) or on another line.
    first = new_return(
        owner_conn, marker, name="a", status=status, outcome=outcome, error_type=error_type
    )

    async def action(conn):
        result = await insert_return(
            conn,
            fields_for(marker, line=line),
            idempotency_key=key_for(marker, "a"),
            payload_hash=HASH_A,
            key_generation=1,
            policy_version=PV,
        )
        await conn.execute("SELECT 1")  # the transaction is still usable
        return result

    result = run(with_conn(app_url, action))
    assert result == KeyConflict()
    assert count_by_key(owner_conn, key_for(marker, "a")) == 1  # no new row
    assert audit_rows(owner_conn, first) == []  # and no R4 audit row
    assert decision_rows(owner_conn, first) == []


# ---------- the blocker that is no longer there ----------


def test_a_blocker_that_leaves_before_the_lookup_gets_the_insert_retried_once(
    marker, owner_conn, owner_database_url, app_url
):
    assert ACTIVE_RETURN_PREDICATE_SQL.strip(), "the hook below recognises the lookup by this text"
    blocker = new_return(owner_conn, marker, name="a", status="processing")

    def the_blocker_fails():
        # lock_timeout: a lock wait here becomes an error instead of a hang.
        with psycopg.connect(
            owner_database_url, autocommit=True, options="-c lock_timeout=5000"
        ) as owner:
            owner.execute(
                "UPDATE public.returns SET status = 'failed', error_type = 'llm_timeout' "
                "WHERE id = %s",
                (blocker,),
            )

    cursor_class, fired, seen = statement_hook(ACTIVE_RETURN_PREDICATE_SQL, the_blocker_fails)
    result = run(
        insert(app_url, fields_for(marker), key_for(marker, "b"), cursor_factory=cursor_class)
    )
    assert len(fired) == 1, "the owner UPDATE ran once, between the collision and the lookup"
    # An INSERT INTO returns ran before the hook (the collision) and another after it (the retry).
    assert any(is_returns_insert(text) for text in seen[: fired[0]]), "no first INSERT INTO returns"
    assert any(is_returns_insert(text) for text in seen[fired[0] :]), "the insert was not retried"
    assert isinstance(result, Inserted)  # the second try finds the order line free
    stored = owner_row(owner_conn, result.row.id)
    assert (stored["status"], stored["attempt"]) == ("processing", 1)
    assert audit_rows(owner_conn, result.row.id) == []
    assert owner_row(owner_conn, blocker)["status"] == "failed"


def test_a_blocker_that_is_not_found_a_second_time_gives_a_deny_with_no_conflicting_id(
    marker, owner_conn, app_url
):
    ids = {}

    async def scenario():
        async with app_connection(app_url, isolation=IsolationLevel.REPEATABLE_READ) as conn:
            await conn.execute("SELECT 1")  # the snapshot is taken here, before the blocker exists
            ids["blocker"] = new_return(owner_conn, marker, name="a", status="processing")
            result = await insert_return(
                conn,
                fields_for(marker),
                idempotency_key=key_for(marker, "b"),
                payload_hash=HASH_B,
                key_generation=1,
                policy_version=PV,
            )
            await conn.commit()
            return result

    result = run(scenario())
    assert isinstance(result, R4Denied)
    stored = owner_row(owner_conn, result.row.id)
    assert (stored["status"], stored["outcome"]) == ("decided", "deny")
    audits = audit_rows(owner_conn, result.row.id)
    assert [a["event_type"] for a in audits] == ["r4_denied"]
    assert audits[0]["conflicting_return_id"] is None  # NULL: fail closed, and nothing was found
    decisions = decision_rows(owner_conn, result.row.id)
    assert len(decisions) == 1
    assert decisions[0]["rule_results"] == R4_ARRAY


# ---------- the same two situations on a retry (failed -> processing) ----------


def test_a_blocker_that_vanishes_during_a_retry_gets_the_update_retried_once(
    marker, owner_conn, owner_database_url, app_url
):
    assert ACTIVE_RETURN_PREDICATE_SQL.strip(), "the hook below recognises the lookup by this text"
    blocker = new_return(owner_conn, marker, name="a", status="processing")
    failed = new_return(
        owner_conn,
        marker,
        name="b",
        status="failed",
        error_type="llm_timeout",
        retry_after_seconds=30,
        attempt=2,
        payload_hash=HASH_B,
    )

    def the_blocker_fails():
        with psycopg.connect(
            owner_database_url, autocommit=True, options="-c lock_timeout=5000"
        ) as owner:
            owner.execute(
                "UPDATE public.returns SET status = 'failed', error_type = 'llm_timeout' "
                "WHERE id = %s",
                (blocker,),
            )

    cursor_class, fired, seen = statement_hook(ACTIVE_RETURN_PREDICATE_SQL, the_blocker_fails)

    async def action(conn):
        return await retry_failed(conn, await get_return(conn, failed))

    result = run(with_conn(app_url, action, cursor_factory=cursor_class))
    assert len(fired) == 1, "the owner UPDATE ran once, between the collision and the lookup"
    # An UPDATE of returns ran before the hook (the collision) and another after it (the retry).
    assert any(is_returns_update(text) for text in seen[: fired[0]]), "no first UPDATE of returns"
    assert any(is_returns_update(text) for text in seen[fired[0] :]), "the update was not retried"
    assert isinstance(result, Retried)  # the second try finds the order line free
    stored = owner_row(owner_conn, failed)
    assert (stored["status"], stored["attempt"]) == ("processing", 3)  # n + 1
    assert stored["outcome"] is None
    assert stored["error_type"] is None
    assert stored["retry_after_seconds"] is None
    assert audit_rows(owner_conn, failed) == []  # entering processing writes no audit row
    assert decision_rows(owner_conn, failed) == []
    assert owner_row(owner_conn, blocker)["status"] == "failed"


def test_a_blocker_that_is_not_found_during_a_retry_gives_a_deny_with_no_conflicting_id(
    marker, owner_conn, app_url
):
    failed = new_return(
        owner_conn,
        marker,
        name="b",
        status="failed",
        error_type="llm_timeout",
        retry_after_seconds=30,
        attempt=2,
        payload_hash=HASH_B,
    )

    async def scenario():
        async with app_connection(app_url, isolation=IsolationLevel.REPEATABLE_READ) as conn:
            await conn.execute("SELECT 1")  # the snapshot is taken here: the failed row is visible
            row = await get_return(conn, failed)
            # The blocker is committed AFTER the snapshot: the unique index sees it (it is not
            # snapshot-based) and the lookup does not.
            new_return(owner_conn, marker, name="a", status="processing")
            result = await retry_failed(conn, row)
            await conn.commit()
            return result

    result = run(scenario())
    assert isinstance(result, R4Denied)
    stored = owner_row(owner_conn, failed)
    assert (stored["status"], stored["outcome"]) == ("decided", "deny")
    assert stored["attempt"] == 3  # n + 1
    assert stored["error_type"] is None
    assert stored["retry_after_seconds"] is None
    decisions = decision_rows(owner_conn, failed)
    assert len(decisions) == 1
    assert decisions[0]["decided_by"] == "rules"
    assert decisions[0]["rule_results"] == R4_ARRAY
    audits = audit_rows(owner_conn, failed)
    assert [(a["event_type"], a["attempt"]) for a in audits] == [("r4_denied", 3)]
    assert audits[0]["conflicting_return_id"] is None  # NULL: fail closed, and nothing was found
