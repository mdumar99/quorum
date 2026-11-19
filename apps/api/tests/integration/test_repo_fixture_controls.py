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
"""

import asyncio
import time
from uuid import uuid4

import psycopg
import pytest
from repo_data import app_connection, new_return, run, wait_until_blocked, with_conn
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
