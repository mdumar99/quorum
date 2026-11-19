"""The three writers that write a status AND its decision in one transaction (issue #56).

Each builder makes the owner-committed rows its write needs (a `processing` return to finalize, or
an active blocker on an order line) and returns a `WriterCase`: the write as an async callable
`action(conn)` for `held_write` or `with_conn`, what another connection sees of the return before
the commit and after it, and who decided. The three are:
- `finalize_decided`: a `processing` return becomes `decided` (approve), decided by the model;
- the R4 deny of `insert_return`: the new return does not exist for anyone else until the commit;
- the R4 deny of `retry_failed`: a `failed` return becomes `decided` (deny) with attempt n + 1.
The blocker of an R4 case is committed by the owner, as in the R4 tests of issue #54. A `tag` keeps
the keys and order lines of two builds in one test apart. Every row carries the run marker.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from repo_data import HASH_B, PV, fields_for, key_for, llm_decision, new_return, owner_row

from app.db.returns import finalize_decided, get_return, insert_return, retry_failed

State = tuple[str, str | None]  # (status, outcome) of a return


@dataclass(frozen=True)
class WriterCase:
    name: str
    action: Callable[[AsyncConnection], Awaitable[object]]
    before: State | None  # what another connection sees before the commit; None: no such row
    after: State  # what it sees after the commit
    decided_by: str  # of the decision the write stores
    decision_outcome: str


def finalize_case(owner, marker: str, tag: str = "x") -> WriterCase:
    return_id = new_return(owner, marker, name=f"fin-{tag}", line=f"fin-{tag}")
    decision = llm_decision("approve")

    async def action(conn):
        return await finalize_decided(conn, return_id=return_id, attempt=1, decision=decision)

    return WriterCase(
        "finalize_decided", action, ("processing", None), ("decided", "approve"), "llm", "approve"
    )


def r4_insert_case(owner, marker: str, tag: str = "x") -> WriterCase:
    line = f"r4i-{tag}"
    new_return(owner, marker, name=f"blk-{tag}", line=line)  # the blocker: active, committed
    fields = fields_for(marker, line=line)

    async def action(conn):
        return await insert_return(
            conn,
            fields,
            idempotency_key=key_for(marker, f"new-{tag}"),
            payload_hash=HASH_B,
            key_generation=1,
            policy_version=PV,
        )

    return WriterCase(
        "r4 deny of insert_return", action, None, ("decided", "deny"), "rules", "deny"
    )


def r4_retry_case(owner, marker: str, tag: str = "x") -> WriterCase:
    line = f"r4r-{tag}"
    new_return(owner, marker, name=f"blk-{tag}", line=line)  # the blocker: active, committed
    failed = new_return(
        owner,
        marker,
        name=f"fail-{tag}",
        line=line,
        status="failed",
        error_type="llm_timeout",
        retry_after_seconds=30,
        payload_hash=HASH_B,
    )

    async def action(conn):
        return await retry_failed(conn, await get_return(conn, failed))

    return WriterCase(
        "r4 deny of retry_failed", action, ("failed", None), ("decided", "deny"), "rules", "deny"
    )


# (pytest id, builder): the three writers, in the order the tests and the controls use them
BUILDERS = [
    ("finalize_decided", finalize_case),
    ("r4-deny-of-insert_return", r4_insert_case),
    ("r4-deny-of-retry_failed", r4_retry_case),
]


def seen_by_owner(owner, return_id: UUID) -> State | None:
    """(status, outcome) of the return as the OWNER connection sees it; None for no such row."""
    row = owner_row(owner, return_id)
    return None if row is None else (row["status"], row["outcome"])
