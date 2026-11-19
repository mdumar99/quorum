"""Guard, marker and cleanup for the integration tests that COMMIT data (issue #54).

The guard `check_test_database` decides whether this database may receive committed test data.
Everything else here sits behind it: the start-up sweep and the cleanup call it first, and refuse to
run if it raises.

The rule (approved): accept `QUORUM_TEST_DB=throwaway` OR `CI=true` (GitHub sets CI itself); an
explicit QUORUM_TEST_DB with any other value is refused even when CI=true. The developer's own
stack never sets either.

Rows made by a test carry a marker at the start of BOTH the idempotency key and the order id:
`p154-<8 hex>-`. The cleanup runs as the owner (quorum_app has no DELETE) and deletes only returns
whose order id AND key start with the marker, in foreign-key order: audit_log, decisions, returns.
It uses `starts_with`, never LIKE, so a marker can contain no wildcard. A start-up sweep removes
stale `p154-` rows left by an interrupted run, and nothing else.
"""

import os
from collections.abc import Mapping
from uuid import uuid4

import psycopg

PREFIX = "p154-"
_MARKER_LENGTH = len(PREFIX) + 8 + 1  # "p154-" + 8 hex + "-"


class DatabaseRefused(Exception):
    """The environment does not say that this database may receive test data that is committed."""


def check_test_database(env: Mapping[str, str]) -> None:
    """Raise DatabaseRefused unless the environment allows committed test data.

    A pure function of the mapping it is given: it never reads os.environ, so a test can pass any
    environment. An explicit QUORUM_TEST_DB decides alone: exactly "throwaway" accepts, any other
    value (the empty string too) refuses, even when CI is "true". Only when QUORUM_TEST_DB is absent
    does CI decide: exactly "true" accepts (GitHub sets it itself), anything else refuses. The
    messages name the variables and never print a value.
    """
    explicit = env.get("QUORUM_TEST_DB")
    if explicit is not None:
        if explicit == "throwaway":
            return
        raise DatabaseRefused(
            "QUORUM_TEST_DB is set to something other than 'throwaway': refusing to write "
            "committed test data (an explicit QUORUM_TEST_DB wins over CI)"
        )
    if env.get("CI") == "true":
        return
    raise DatabaseRefused(
        "neither QUORUM_TEST_DB=throwaway nor CI=true is set: refusing to write committed test "
        "data to a database that may not be a throwaway one"
    )


def new_run_marker() -> str:
    """A marker for one test: `p154-` and 8 hex characters and a dash."""
    return f"{PREFIX}{uuid4().hex[:8]}-"


def _delete_marked(owner_conn: psycopg.Connection, prefix: str) -> None:
    """Delete the returns whose order id AND key start with `prefix`, and their rows."""
    marked = (
        "SELECT id FROM public.returns "
        "WHERE starts_with(order_id, %(p)s) AND starts_with(idempotency_key, %(p)s)"
    )
    owner_conn.execute(
        f"DELETE FROM public.audit_log WHERE return_id IN ({marked}) "
        f"OR conflicting_return_id IN ({marked})",
        {"p": prefix},
    )
    owner_conn.execute(f"DELETE FROM public.decisions WHERE return_id IN ({marked})", {"p": prefix})
    owner_conn.execute(f"DELETE FROM public.returns WHERE id IN ({marked})", {"p": prefix})
    owner_conn.commit()


def delete_marked(
    owner_conn: psycopg.Connection, marker: str, env: Mapping[str, str] | None = None
) -> None:
    """The cleanup of one test. Behind the guard; refuses a marker that is not a full marker (an
    empty or short prefix would match every row)."""
    check_test_database(os.environ if env is None else env)
    if not (marker.startswith(PREFIX) and len(marker) >= _MARKER_LENGTH):
        raise ValueError("not a run marker: refusing to delete")
    _delete_marked(owner_conn, marker)


def sweep(owner_conn: psycopg.Connection, env: Mapping[str, str] | None = None) -> None:
    """The start-up sweep: every stale `p154-` row of an earlier or interrupted run. Behind the
    guard."""
    check_test_database(os.environ if env is None else env)
    _delete_marked(owner_conn, PREFIX)
