"""Controls on the fixture itself, and proof of the role the tests run as (issue #54, criterion 9).

The committing tests delete their rows as the OWNER, so the cleanup must be able to hurt. These
tests show that it deletes only what it should:
- an unmarked return, decision and audit row survive both the cleanup and the sweep, and so do
  rows marked in only one of the two places (order id or key): both must carry the marker;
- the cleanup removes marked rows in foreign-key order without a foreign-key error;
- the sweep removes stale `p154-` rows of another run, and nothing else;
- both are behind the guard, and a marker that is not a full marker deletes nothing.
They also prove that "ran as quorum_app" is true: the role, no superuser, no DELETE, and an
ungranted column fails with `permission denied`.

Finally they control the race helpers themselves, because a helper that cannot see a block makes
every race test fail or hang for the wrong reason (the red run could not show this: the stubbed
calls died before anything could block). `wait_until_blocked` sees a real block, gives up when
nothing blocks, and a lock wait that nobody releases ends in `LockNotAvailable` and not in a hang.

The last five tests control `held_write` (issue #56), the helper that runs a repository write on its
own connection and keeps its transaction open until the test commits it. The red run cannot show
that it works (the function under test is a stub), so they read through the OWNER only and do not
touch `get_decision`: another connection sees nothing until the commit and everything after it;
leaving without a commit leaves nothing; the lock is released at exit even when the body raises
(a failure is a `LockNotAvailable` after 2 s, not a hang); and all of it holds for each of the three
writers that write a status and its decision together.
"""

import asyncio
import time
from uuid import uuid4

import psycopg
import pytest
from repo_cases import BUILDERS, finalize_case, seen_by_owner
from repo_data import (
    app_connection,
    decision_rows,
    held_write,
    new_return,
    run,
    wait_until_blocked,
    with_conn,
)
from repo_fixtures import PREFIX, DatabaseRefused, delete_marked, sweep
from schema_helpers import POLICY_VERSION, insert_audit, insert_decision
from schema_helpers import insert_return as owner_insert

pytestmark = pytest.mark.integration

STALE_MARKER = f"{PREFIX}deadbeef-"


def unmarked_name() -> str:
    return f"unmarked-{uuid4().hex[:8]}"


def count(owner, table: str, column: str, value) -> int:
    assert table in ("returns", "decisions", "audit_log")
    query = f"SELECT count(*) FROM public.{table} WHERE {column} = %s"
    n = owner.execute(query, (value,)).fetchone()
    owner.rollback()
    return n[0]


def remove_by_id(owner, return_ids) -> None:
    """The test's own cleanup of rows the fixture must NOT delete: by id, in FK order."""
    for return_id in return_ids:
        owner.execute(
            "DELETE FROM public.audit_log WHERE return_id = %s OR conflicting_return_id = %s",
            (return_id, return_id),
        )
        owner.execute("DELETE FROM public.decisions WHERE return_id = %s", (return_id,))
        owner.execute("DELETE FROM public.returns WHERE id = %s", (return_id,))
    owner.commit()


def make(owner, order_id: str, key: str):
    return_id = owner_insert(
        owner, order_id=order_id, idempotency_key=key, status="decided", outcome="deny"
    )
    insert_decision(owner, return_id)
    insert_audit(owner, return_id, event_type="rules_decided")
    owner.commit()
    return return_id


# ---------- what must survive ----------


def test_unmarked_rows_survive_the_cleanup_and_the_sweep(marker, owner_conn):
    name = unmarked_name()
    unmarked = make(owner_conn, name, name)
    only_order_marked = make(owner_conn, f"{marker}order", unmarked_name())
    only_key_marked = make(owner_conn, unmarked_name(), f"{marker}key")
    survivors = [unmarked, only_order_marked, only_key_marked]
    try:
        delete_marked(owner_conn, marker)
        sweep(owner_conn)
        for return_id in survivors:
            assert count(owner_conn, "returns", "id", return_id) == 1
            assert count(owner_conn, "decisions", "return_id", return_id) == 1
            assert count(owner_conn, "audit_log", "return_id", return_id) == 1
    finally:
        remove_by_id(owner_conn, survivors)


# ---------- what must go ----------


def test_the_cleanup_removes_marked_rows_in_fk_order_without_an_fk_error(marker, owner_conn):
    first = new_return(owner_conn, marker, name="a", policy_version=POLICY_VERSION)
    second = new_return(owner_conn, marker, name="b", line="2", policy_version=POLICY_VERSION)
    insert_decision(owner_conn, first)
    insert_audit(owner_conn, first, event_type="rules_decided")
    insert_audit(owner_conn, second, event_type="r4_denied", conflicting_return_id=first)
    owner_conn.commit()
    delete_marked(owner_conn, marker)  # must not raise a foreign-key error
    for return_id in (first, second):
        assert count(owner_conn, "returns", "id", return_id) == 0
        assert count(owner_conn, "decisions", "return_id", return_id) == 0
        assert count(owner_conn, "audit_log", "return_id", return_id) == 0
    assert count(owner_conn, "audit_log", "conflicting_return_id", first) == 0


def test_the_sweep_removes_stale_marked_rows_of_another_run_and_nothing_else(marker, owner_conn):
    stale = make(owner_conn, f"{STALE_MARKER}order", f"{STALE_MARKER}key")
    name = unmarked_name()
    unmarked = make(owner_conn, name, name)
    try:
        sweep(owner_conn)
        assert count(owner_conn, "returns", "id", stale) == 0
        assert count(owner_conn, "decisions", "return_id", stale) == 0
        assert count(owner_conn, "audit_log", "return_id", stale) == 0
        assert count(owner_conn, "returns", "id", unmarked) == 1
    finally:
        remove_by_id(owner_conn, [stale, unmarked])


# ---------- the guard and the marker ----------


def test_the_sweep_and_the_cleanup_are_behind_the_guard(marker, owner_conn):
    return_id = new_return(owner_conn, marker)
    with pytest.raises(DatabaseRefused):
        delete_marked(owner_conn, marker, env={})
    with pytest.raises(DatabaseRefused):
        sweep(owner_conn, env={})
    assert count(owner_conn, "returns", "id", return_id) == 1  # nothing was deleted


@pytest.mark.parametrize("bad", ["", "p154-", "p154-abc", "p154-12345678", "other-12345678-"])
def test_a_marker_that_is_not_a_full_marker_deletes_nothing(marker, owner_conn, bad):
    return_id = new_return(owner_conn, marker)
    with pytest.raises(ValueError):
        delete_marked(owner_conn, bad)
    assert count(owner_conn, "returns", "id", return_id) == 1


def test_the_marker_is_a_full_marker(marker):
    assert marker.startswith(PREFIX)
    assert len(marker) == len(PREFIX) + 8 + 1
    assert marker.endswith("-")


# ---------- the role the tests run as ----------


def test_the_app_connection_is_quorum_app_and_not_a_superuser(app_url):
    async def who(conn):
        cursor = await conn.execute(
            "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
        return await cursor.fetchone()

    assert run(with_conn(app_url, who, commit=False)) == ("quorum_app", False)


def test_the_owner_connection_is_a_different_role(owner_conn):
    assert owner_conn.execute("SELECT current_user").fetchone()[0] == "quorum_owner"
    owner_conn.rollback()


def test_an_ungranted_update_fails_with_permission_denied(app_url):
    async def update(conn):
        # Column privileges are checked when the statement is planned, even for zero rows.
        await conn.execute("UPDATE public.returns SET as_of = now() WHERE false")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(with_conn(app_url, update, commit=False))


def test_a_granted_update_of_zero_rows_works(app_url):
    async def update(conn):
        await conn.execute(
            "UPDATE public.returns SET status = status, updated_at = now() WHERE false"
        )

    run(with_conn(app_url, update, commit=False))


def test_the_app_role_has_no_delete_so_the_cleanup_needs_the_owner(app_url):
    async def delete(conn):
        await conn.execute("DELETE FROM public.returns WHERE false")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(with_conn(app_url, delete, commit=False))


# ---------- the race helpers ----------

TOUCH_ROW = "UPDATE public.returns SET status = status, updated_at = now() WHERE id = %s"


def test_a_real_block_is_seen_by_wait_until_blocked(
    marker, owner_conn, owner_database_url, app_url
):
    return_id = new_return(owner_conn, marker, status="processing")

    async def scenario():
        async with app_connection(app_url) as a, app_connection(app_url) as b:
            await a.execute(TOUCH_ROW, (return_id,))  # A holds the row lock and stays open
            task = asyncio.create_task(b.execute(TOUCH_ROW, (return_id,)))
            await wait_until_blocked(owner_database_url, b.info.backend_pid)
            assert not task.done()  # B is really waiting, not finished
            await a.rollback()  # A lets go
            await task  # B finishes
            await b.rollback()

    run(scenario())


def test_nothing_blocks_so_wait_until_blocked_gives_up(owner_database_url, app_url):
    async def scenario():
        async with app_connection(app_url) as idle:
            started = time.monotonic()
            with pytest.raises(AssertionError, match="never waited on a lock"):
                await wait_until_blocked(owner_database_url, idle.info.backend_pid, timeout=0.5)
            return time.monotonic() - started

    assert run(scenario()) < 2.0  # the 0.5 s timeout, not a hang


def test_a_wait_nobody_releases_ends_in_lock_not_available(
    marker, owner_conn, owner_database_url, app_url
):
    return_id = new_return(owner_conn, marker, status="processing")

    async def scenario():
        async with (
            app_connection(app_url) as a,
            app_connection(app_url, lock_timeout_ms=500) as b,
        ):
            await a.execute(TOUCH_ROW, (return_id,))  # A holds the row lock and never releases it
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                await b.execute(TOUCH_ROW, (return_id,))
            elapsed = time.monotonic() - started
            await a.rollback()
            return elapsed

    assert run(scenario()) < 3.0  # about 0.5 s: an error, not a hang


# ---------- the held write (issue #56) ----------


async def own_decision_count(held, return_id) -> int:
    """How many decision rows the held connection ITSELF sees for the return: 1 proves that the
    held write really wrote its decision, so "invisible to others" is not "nothing happened"."""
    cursor = await held.conn.execute(
        "SELECT count(*) FROM public.decisions WHERE return_id = %s", (return_id,)
    )
    return (await cursor.fetchone())[0]


def test_a_held_write_is_invisible_to_another_connection_until_it_commits(
    marker, owner_conn, app_url
):
    case = finalize_case(owner_conn, marker, "h1")

    async def scenario():
        async with held_write(app_url, case.action) as held:
            return_id = held.result.row.id
            own = await own_decision_count(held, return_id)
            others = (
                seen_by_owner(owner_conn, return_id),
                len(decision_rows(owner_conn, return_id)),
            )
        return own, others

    own, others = run(scenario())
    assert own == 1
    assert others == (("processing", None), 0)


def test_a_held_write_is_visible_to_another_connection_once_it_commits(marker, owner_conn, app_url):
    case = finalize_case(owner_conn, marker, "h2")

    async def scenario():
        async with held_write(app_url, case.action) as held:
            return_id = held.result.row.id
            await held.commit()
            return seen_by_owner(owner_conn, return_id), len(decision_rows(owner_conn, return_id))

    assert run(scenario()) == (("decided", "approve"), 1)


def test_a_held_write_that_is_not_committed_leaves_nothing_behind_at_exit(
    marker, owner_conn, app_url
):
    case = finalize_case(owner_conn, marker, "h3")

    async def scenario():
        async with held_write(app_url, case.action) as held:
            return held.result.row.id

    return_id = run(scenario())
    assert seen_by_owner(owner_conn, return_id) == ("processing", None)
    assert decision_rows(owner_conn, return_id) == []


def test_a_held_write_releases_its_locks_at_exit_even_when_the_body_raises(
    marker, owner_conn, owner_database_url, app_url
):
    case = finalize_case(owner_conn, marker, "h4")
    held_id = []

    def touch_as_the_owner():
        """An owner UPDATE of the held row, with a 2 s lock_timeout: a wait ends in an error."""
        with psycopg.connect(
            owner_database_url, autocommit=True, options="-c lock_timeout=2000"
        ) as conn:
            sql = "UPDATE public.returns SET updated_at = now() WHERE id = %s"
            return conn.execute(sql, (held_id[0],)).rowcount

    async def scenario():
        with pytest.raises(RuntimeError, match="the body failed"):
            async with held_write(app_url, case.action) as held:
                held_id.append(held.result.row.id)
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    touch_as_the_owner()  # while it is held the lock is real
                raise RuntimeError("the body failed")
        return touch_as_the_owner()  # after the exit it is released: no wait, one row changed

    assert run(scenario()) == 1


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in BUILDERS])
def test_a_held_write_works_for_each_of_the_three_writers(build, marker, owner_conn, app_url):
    dropped = build(owner_conn, marker, "one")
    kept = build(owner_conn, marker, "two")

    async def hold(case, commit):
        async with held_write(app_url, case.action) as held:
            return_id = held.result.row.id
            own = await own_decision_count(held, return_id)
            during = (
                seen_by_owner(owner_conn, return_id),
                len(decision_rows(owner_conn, return_id)),
            )
            after = None
            if commit:
                await held.commit()
                after = (
                    seen_by_owner(owner_conn, return_id),
                    len(decision_rows(owner_conn, return_id)),
                )
        return return_id, own, during, after

    dropped_id, own, during, _ = run(hold(dropped, commit=False))
    assert (own, during) == (1, (dropped.before, 0))  # held: the owner sees the old state
    assert (seen_by_owner(owner_conn, dropped_id), len(decision_rows(owner_conn, dropped_id))) == (
        dropped.before,
        0,
    )  # left without a commit: nothing

    _kept_id, own, during, after = run(hold(kept, commit=True))
    assert (own, during) == (1, (kept.before, 0))
    assert after == (kept.after, 1)  # committed: the new state and exactly one decision row


def test_a_held_write_whose_action_raises_after_writing_passes_the_error_on_and_releases_the_row(
    marker, owner_conn, owner_database_url, app_url
):
    """H7: the action takes the row lock through the held connection and then raises. `held_write`
    must pass the error on, close that connection and so release the lock: an owner UPDATE of the
    row with a 2 s lock_timeout then succeeds. A helper that runs the action outside the
    protection of the connection's own exit would leak the connection and its lock here (a
    LockNotAvailable)."""
    return_id = new_return(owner_conn, marker, name="h7", line="h7")
    held_connections = []

    async def action(conn):
        held_connections.append(conn)
        await conn.execute(
            "UPDATE public.returns SET updated_at = now() WHERE id = %s", (return_id,)
        )  # the write: the row lock is taken on the held connection
        raise RuntimeError("the action failed after writing")

    def touch_as_the_owner():
        with psycopg.connect(
            owner_database_url, autocommit=True, options="-c lock_timeout=2000"
        ) as conn:
            sql = "UPDATE public.returns SET updated_at = now() WHERE id = %s"
            return conn.execute(sql, (return_id,)).rowcount

    async def scenario():
        try:
            with pytest.raises(RuntimeError, match="the action failed after writing"):
                async with held_write(app_url, action):
                    pytest.fail("the body must not run when the action raised")
            rowcount = touch_as_the_owner()  # in a leak this is the LockNotAvailable
            assert [conn.closed for conn in held_connections] == [True]
            return rowcount
        finally:  # a control that fails must not leave its lock behind for the cleanup to wait on
            for conn in held_connections:
                if not conn.closed:
                    await conn.rollback()
                    await conn.close()

    assert run(scenario()) == 1


def test_a_held_write_that_is_left_without_a_commit_closes_its_connection_and_releases_its_row(
    marker, owner_conn, owner_database_url, app_url
):
    """The normal exit; control 4 is the exit of a body that raises."""
    case = finalize_case(owner_conn, marker, "h8")

    async def scenario():
        async with held_write(app_url, case.action) as held:
            return_id, conn = held.result.row.id, held.conn
        assert conn.closed  # a connection left open is the leak, whether or not GC closes it later
        with psycopg.connect(
            owner_database_url, autocommit=True, options="-c lock_timeout=2000"
        ) as owner:
            sql = "UPDATE public.returns SET updated_at = now() WHERE id = %s"
            return owner.execute(sql, (return_id,)).rowcount

    assert run(scenario()) == 1
