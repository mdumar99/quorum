"""`get_decision`: the stored decision of a return, read as quorum_app (issue #56).

Criteria 1 to 6 of the issue. Setup is done with the 12a functions (so the rows are the ones the
repository really writes) or, where a stored value cannot come from them, by the owner. The function
under test runs as `quorum_app` on its own connection. Rows carry the run marker; the owner removes
them afterwards. Every connection that can block has a `lock_timeout` of 2 s, and there is no sleep
and no wait helper: a reader never waits for an uncommitted writer, so a wait that does happen ends
in `LockNotAvailable` and not in a hang.

The contract the function documents (criterion 6): a return and its decision are committed in one
transaction; a caller reads the return first and the decision after it, and shows the decision only
when the status read says `decided` or `pending_review`. The gate is 12b's and is tested there; the
tests here assert only what the function RETURNS, for the three writers that write a status and its
decision together (`finalize_decided`, the R4 deny of `insert_return` and of `retry_failed`).

"Writes nothing" is read on the READER'S OWN connection, in its transaction, before and after the
call: counts of the three tables and the `xmin` of the return row and of the decision row. An
UPDATE or INSERT the function made and did not commit is invisible to an owner connection and
visible to the connection that made it.
"""

from uuid import uuid4

import pytest
from psycopg.pq import TransactionStatus
from repo_cases import BUILDERS, r4_insert_case, r4_retry_case, seen_by_owner
from repo_data import (
    LLM_COLUMNS,
    PV,
    R4_ARRAY,
    app_connection,
    decision_rows,
    held_write,
    is_plain_select,
    llm_call,
    llm_decision,
    new_return,
    policy_result,
    rules_decision,
    run,
    statement_hook,
    table_state,
    with_conn,
)
from schema_helpers import insert_decision

from app.db.models import Applied, DecisionRecord, DecisionRow
from app.db.returns import (
    DECISION_BY_RETURN_SQL,
    DECISION_COLUMNS,
    finalize_decided,
    get_decision,
    get_return,
)
from app.llm.capture import capture_raw_output

pytestmark = pytest.mark.integration

LOCK_MS = 2000  # every connection that can block gives up after 2 s


# ---------- helpers ----------


def read(url, return_id):
    """`get_decision` on its own connection."""

    async def go():
        async with app_connection(url, lock_timeout_ms=LOCK_MS) as conn:
            return await get_decision(conn, return_id)

    return run(go())


def decide(url, return_id, decision, attempt=1):
    """The real `finalize_decided`, committed."""

    async def action(conn):
        return await finalize_decided(conn, return_id=return_id, attempt=attempt, decision=decision)

    return run(with_conn(url, action, lock_timeout_ms=LOCK_MS))


def model_call_columns(row: DecisionRow) -> dict:
    return {column: getattr(row, column) for column in LLM_COLUMNS}


def assert_equals_the_owners_read(row, return_id, owner_conn):
    """The row is exactly what `SELECT *` as the owner sees: the 16 columns, every value."""
    stored = decision_rows(owner_conn, return_id)
    assert len(stored) == 1
    assert tuple(stored[0]) == DECISION_COLUMNS
    assert row.model_dump() == stored[0]


def assert_reads_back_as_stored(row, decision, return_id, owner_conn):
    """All 16 columns, against what the 12a function was given and against the owner's own read."""
    assert isinstance(row, DecisionRow)
    result = decision.policy_result
    assert row.return_id == return_id
    assert row.policy_version == result.policy_version
    assert (row.outcome, row.decided_by) == (decision.outcome, decision.decided_by)
    assert row.rule_results == result.rule_results_json()
    assert row.allowed_outcomes == list(result.allowed_outcomes)
    expected_call = decision.llm.columns() if decision.llm else dict.fromkeys(LLM_COLUMNS)
    assert model_call_columns(row) == expected_call
    assert_equals_the_owners_read(row, return_id, owner_conn)


def state_of(return_row):
    """(status, outcome) of a return read by `get_return`; None when there is no such return."""
    return None if return_row is None else (return_row.status, return_row.outcome)


def make_a_bystander(owner_conn, marker, app_url):
    """A decided return WITH its decision (the real `finalize_decided`, committed), on its own
    line. It is made FIRST in every test that expects None: a read that lost its
    `WHERE return_id = %s` would return this row, so those tests go red on their own, and not
    only the tests that compare two decisions."""
    bystander = new_return(owner_conn, marker, name="bystander", line="bystander")
    decide(app_url, bystander, llm_decision("approve"))
    return bystander


# ---------- criterion 1: no decision, no row ----------


def test_an_id_that_does_not_exist_has_no_decision(marker, owner_conn, app_url):
    make_a_bystander(owner_conn, marker, app_url)
    assert read(app_url, uuid4()) is None


def test_a_processing_return_has_no_decision(marker, owner_conn, app_url):
    make_a_bystander(owner_conn, marker, app_url)
    return_id = new_return(owner_conn, marker)
    assert read(app_url, return_id) is None


@pytest.mark.parametrize(
    ("error_type", "retry_after_seconds"),
    [("llm_timeout", 30), ("invalid_llm_output", None)],
    ids=["a-transient-failure", "a-permanent-failure"],
)
def test_a_failed_return_has_no_decision(
    error_type, retry_after_seconds, marker, owner_conn, app_url
):
    make_a_bystander(owner_conn, marker, app_url)
    return_id = new_return(
        owner_conn,
        marker,
        status="failed",
        error_type=error_type,
        retry_after_seconds=retry_after_seconds,
    )
    assert read(app_url, return_id) is None


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("decided", "approve"), ("pending_review", "escalate")],
    ids=["a-decided-return", "a-pending-review-return"],
)
def test_a_decided_or_pending_review_return_with_no_decision_row_reads_as_none(
    status, outcome, marker, owner_conn, app_url
):
    """The edge case 12b shows as `decision: null`: nothing in the schema forces a decision row for
    a `decided` or `pending_review` return, and only the repository writes both together. A row the
    owner inserts directly (as the schema tests do) has none, so the read returns None for it and
    the caller, whose status read said `decided` or `pending_review`, shows no decision."""
    make_a_bystander(owner_conn, marker, app_url)
    return_id = new_return(owner_conn, marker, status=status, outcome=outcome)
    assert decision_rows(owner_conn, return_id) == []  # the premise: it really has no decision row
    assert read(app_url, return_id) is None


# ---------- criterion 2: the stored row, all 16 columns ----------

DECISIONS = [
    pytest.param(lambda: rules_decision("deny", "digital"), id="a-rules-deny"),
    pytest.param(lambda: llm_decision("approve"), id="an-llm-approve"),
    pytest.param(lambda: llm_decision("deny"), id="an-llm-deny"),
    pytest.param(lambda: llm_decision("escalate"), id="an-llm-escalate-which-is-pending-review"),
]


@pytest.mark.parametrize("make_decision", DECISIONS)
def test_a_stored_decision_reads_back_field_for_field(make_decision, marker, owner_conn, app_url):
    return_id = new_return(owner_conn, marker)
    decision = make_decision()
    assert isinstance(decide(app_url, return_id, decision), Applied)
    row = read(app_url, return_id)
    assert_reads_back_as_stored(row, decision, return_id, owner_conn)


@pytest.mark.parametrize(
    "build", [r4_insert_case, r4_retry_case], ids=["of-insert_return", "of-retry_failed"]
)
def test_an_r4_deny_reads_back_with_the_one_r4_entry_and_no_model_call(
    build, marker, owner_conn, app_url
):
    case = build(owner_conn, marker)
    result = run(with_conn(app_url, case.action, lock_timeout_ms=LOCK_MS))
    return_id = result.row.id
    row = read(app_url, return_id)
    assert (row.decided_by, row.outcome) == ("rules", "deny")
    assert row.allowed_outcomes == ["deny"]
    assert row.rule_results == R4_ARRAY
    assert row.policy_version == PV
    assert model_call_columns(row) == dict.fromkeys(LLM_COLUMNS)
    assert_equals_the_owners_read(row, return_id, owner_conn)


def test_the_raw_output_reads_back_exactly_as_stored(marker, owner_conn, app_url):
    raw = '{"outcome": "approve", "rationale": "café ✓"}  \n\n'
    decision = DecisionRecord(
        outcome="approve", decided_by="llm", policy_result=policy_result(), llm=llm_call(raw)
    )
    capture = capture_raw_output(raw)
    return_id = new_return(owner_conn, marker)
    decide(app_url, return_id, decision)
    row = read(app_url, return_id)
    assert row.raw_output == raw  # not stripped, not re-encoded
    assert (row.raw_output_bytes, row.raw_output_sha256, row.raw_output_truncated) == (
        capture.original_bytes,
        capture.sha256,
        False,
    )
    assert_reads_back_as_stored(row, decision, return_id, owner_conn)


def test_a_capped_raw_output_reads_back_capped_with_its_original_size_hash_and_flag(
    marker, owner_conn, app_url
):
    raw = '{"outcome": "approve", "rationale": "' + "é" * 10_000 + '"}'
    capture = capture_raw_output(raw)
    assert capture.truncated and capture.original_bytes > 16_384  # the premise of this test
    decision = DecisionRecord(
        outcome="approve", decided_by="llm", policy_result=policy_result(), llm=llm_call(raw)
    )
    return_id = new_return(owner_conn, marker)
    decide(app_url, return_id, decision)
    row = read(app_url, return_id)
    assert row.raw_output == capture.text
    assert len(row.raw_output.encode()) <= 16_384
    assert row.raw_output_bytes == capture.original_bytes
    assert row.raw_output_sha256 == capture.sha256
    assert row.raw_output_truncated is True
    assert_reads_back_as_stored(row, decision, return_id, owner_conn)


def test_allowed_outcomes_read_back_in_the_order_they_were_stored(marker, owner_conn, app_url):
    """Rows the 12a functions write are already alphabetical, so only a row the owner inserts can
    show that the order is kept as stored."""
    stored_order = ["escalate", "deny", "approve"]
    assert stored_order != sorted(stored_order)  # the premise of this test
    return_id = new_return(owner_conn, marker, status="pending_review", outcome="escalate")
    insert_decision(
        owner_conn, return_id, policy_version=PV, outcome="escalate", allowed_outcomes=stored_order
    )
    owner_conn.commit()
    assert read(app_url, return_id).allowed_outcomes == stored_order


def test_each_return_reads_its_own_decision(marker, owner_conn, app_url):
    first = new_return(owner_conn, marker, name="1", line="own-1")
    second = new_return(owner_conn, marker, name="2", line="own-2")
    deny, approve = rules_decision("deny", "digital"), llm_decision("approve")
    decide(app_url, first, deny)
    decide(app_url, second, approve)
    assert_reads_back_as_stored(read(app_url, first), deny, first, owner_conn)
    assert_reads_back_as_stored(read(app_url, second), approve, second, owner_conn)


# ---------- criteria 3 and 4: one plain SELECT, and nothing written ----------


def make_subject(kind, owner_conn, marker, app_url):
    """The return id of a decided return, a processing one, or one that does not exist."""
    if kind == "decided":
        return_id = new_return(owner_conn, marker)
        decide(app_url, return_id, llm_decision("approve"))
        return return_id
    make_a_bystander(owner_conn, marker, app_url)  # so that a read of the wrong row is seen
    if kind == "processing":
        return new_return(owner_conn, marker)
    return uuid4()


SUBJECTS = [
    pytest.param("decided", id="a-decided-return-with-its-decision"),
    pytest.param("processing", id="a-processing-return-with-no-decision"),
    pytest.param("unknown", id="an-id-that-does-not-exist"),
]


async def observe_one_read(url, return_id):
    """One `get_decision` on a reader connection that records every statement it runs. Returns the
    result, the state of the tables as THIS connection saw it before and after, the statements the
    function ran (not the ones that read the state), the transaction status RIGHT AFTER the read
    (before any other statement: a commit inside the function would be hidden by the next
    statement, which opens a new transaction) and the status after an explicit commit."""
    cursor_class, _fired, seen = statement_hook("", lambda: None)  # records, never fires
    async with app_connection(url, cursor_factory=cursor_class, lock_timeout_ms=LOCK_MS) as reader:
        before = await table_state(reader, return_id)
        start = len(seen)
        result = await get_decision(reader, return_id)
        status = reader.info.transaction_status  # immediately: nothing runs in between
        ran = seen[start:]
        after = await table_state(reader, return_id)
        await reader.commit()
        return result, before, after, ran, status, reader.info.transaction_status


@pytest.mark.parametrize("kind", SUBJECTS)
def test_the_read_runs_exactly_one_statement_and_it_is_the_plain_select_of_the_decision(
    kind, marker, owner_conn, app_url
):
    return_id = make_subject(kind, owner_conn, marker, app_url)
    _result, _before, _after, ran, _status, _idle = run(observe_one_read(app_url, return_id))
    assert ran == [DECISION_BY_RETURN_SQL]
    assert is_plain_select(ran[0])


@pytest.mark.parametrize("kind", SUBJECTS)
def test_the_read_changes_no_count_and_no_row_version_as_its_own_transaction_sees_them(
    kind, marker, owner_conn, app_url
):
    return_id = make_subject(kind, owner_conn, marker, app_url)
    _result, before, after, _ran, _status, _idle = run(observe_one_read(app_url, return_id))
    assert before == after
    # the premise: the state really was read, so that "unchanged" is not "both empty"
    assert before["returns_count"] >= (0 if kind == "unknown" else 1)
    assert (before["return_xmin"] is None) == (kind == "unknown")
    assert (before["decision_xmin"] is not None) == (kind == "decided")


@pytest.mark.parametrize("kind", SUBJECTS)
def test_the_read_leaves_the_callers_transaction_open_and_commits_nothing(
    kind, marker, owner_conn, app_url
):
    return_id = make_subject(kind, owner_conn, marker, app_url)
    _result, _before, _after, _ran, after_the_read, after_a_commit = run(
        observe_one_read(app_url, return_id)
    )
    assert after_the_read == TransactionStatus.INTRANS  # a commit inside the read would end it
    assert after_a_commit == TransactionStatus.IDLE  # the control: a commit does end it


# ---------- criterion 5: an autocommit connection is not refused ----------


def test_the_read_gives_the_same_row_on_an_autocommit_connection_and_opens_no_savepoint(
    marker, owner_conn, app_url
):
    return_id = new_return(owner_conn, marker)
    decision = llm_decision("approve")
    decide(app_url, return_id, decision)
    on_a_normal_connection = read(app_url, return_id)

    async def on_an_autocommit_connection():
        cursor_class, _fired, seen = statement_hook("", lambda: None)
        async with app_connection(
            app_url, cursor_factory=cursor_class, lock_timeout_ms=LOCK_MS, autocommit=True
        ) as conn:
            result = await get_decision(conn, return_id)
            return result, list(seen), conn.info.transaction_status

    result, ran, status = run(on_an_autocommit_connection())
    assert result == on_a_normal_connection
    assert_reads_back_as_stored(result, decision, return_id, owner_conn)
    assert ran == [DECISION_BY_RETURN_SQL]  # one statement, no SAVEPOINT
    assert status == TransactionStatus.IDLE  # autocommit: nothing is left open


# ---------- criterion 6: the contract, for the three writers ----------

WRITERS = [pytest.param(build, id=name) for name, build in BUILDERS]


@pytest.mark.parametrize("build", WRITERS)
def test_after_a_committed_write_the_return_reads_as_decided_and_its_decision_reads_back(
    build, marker, owner_conn, app_url
):
    case = build(owner_conn, marker, "a")

    async def go():
        async with held_write(app_url, case.action) as held:
            return_id = held.result.row.id
            await held.commit()
        async with app_connection(app_url, lock_timeout_ms=LOCK_MS) as reader:
            return (
                return_id,
                await get_return(reader, return_id),
                await get_decision(reader, return_id),
            )

    return_id, return_row, decision = run(go())
    assert state_of(return_row) == case.after
    assert decision is not None
    assert (decision.return_id, decision.decided_by, decision.outcome) == (
        return_id,
        case.decided_by,
        case.decision_outcome,
    )


@pytest.mark.parametrize("build", WRITERS)
def test_while_the_write_is_held_the_decision_reads_as_none_and_the_return_shows_its_old_state(
    build, marker, owner_conn, app_url
):
    case = build(owner_conn, marker, "b")

    async def go():
        async with held_write(app_url, case.action) as held:
            return_id = held.result.row.id
            cursor = await held.conn.execute(
                "SELECT count(*) FROM public.decisions WHERE return_id = %s", (return_id,)
            )
            held_sees = (await cursor.fetchone())[0]
            async with app_connection(app_url, lock_timeout_ms=LOCK_MS) as reader:
                return_row = await get_return(reader, return_id)
                decision = await get_decision(reader, return_id)
        return return_id, held_sees, return_row, decision

    return_id, held_sees, return_row, decision = run(go())
    assert held_sees == 1  # the premise: the held write really wrote its decision
    assert state_of(return_row) == case.before
    assert decision is None
    assert read(app_url, return_id) is None  # and after the exit, which rolled it back
    assert seen_by_owner(owner_conn, return_id) == case.before


@pytest.mark.parametrize("build", WRITERS)
def test_a_commit_between_the_two_reads_shows_in_the_decision_read_on_the_same_connection(
    build, marker, owner_conn, app_url
):
    """The documented rule tells the caller to ignore the row when its status read said anything
    but `decided` or `pending_review`: here the status read, made before the commit, did not. The
    reader is ONE connection with ONE open transaction across the commit, so each statement takes
    a fresh snapshot (READ COMMITTED, the repository's documented assumption)."""
    case = build(owner_conn, marker, "c")

    async def go():
        async with app_connection(app_url, lock_timeout_ms=LOCK_MS) as reader:
            async with held_write(app_url, case.action) as held:
                return_id = held.result.row.id
                first = await get_return(reader, return_id)
                open_before = reader.info.transaction_status
                await held.commit()
                decision = await get_decision(reader, return_id)
                second = await get_return(reader, return_id)
            return first, open_before, decision, second, reader.info.transaction_status

    first, open_before, decision, second, open_after = run(go())
    assert state_of(first) == case.before  # the status read did not show decided or pending_review
    assert open_before == TransactionStatus.INTRANS  # the premise: the transaction was open
    assert decision is not None  # the decision read, later in the same transaction, sees the commit
    assert (decision.decided_by, decision.outcome) == (case.decided_by, case.decision_outcome)
    assert state_of(second) == case.after
    assert open_after == TransactionStatus.INTRANS  # one transaction throughout
