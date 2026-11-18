"""Reads of the `returns` table (issue #50). The writes and the transactions are P1-12's.

The functions take a CONNECTION: the caller owns `async with pool.connection()`, so it also owns the
transaction and can run several statements in one (P1-12 writes returns.outcome and the decision
together). A connection is only ever held inside such a short block, never across an LLM call.
"""

from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import class_row

from app.db.models import ReturnRow

# The columns of public.returns in table order, written out so that a test can compare three
# independent things: this list, the model's fields and the table in db/schema.sql.
RETURN_COLUMNS: tuple[str, ...] = (
    "id",
    "order_id",
    "order_line_id",
    "category",
    "amount_minor",
    "currency",
    "delivered_on",
    "reason",
    "status",
    "outcome",
    "error_type",
    "retry_after_seconds",
    "attempt",
    "as_of",
    "policy_version",
    "idempotency_key",
    "payload_hash",
    "key_generation",
    "created_at",
    "updated_at",
)
# An explicit column list, never SELECT *: a schema change then fails loudly instead of silently
# adding or losing a field. Built from the constant above, which holds no user input; the id is a
# bound parameter.
RETURN_BY_ID_SQL = f"SELECT {', '.join(RETURN_COLUMNS)} FROM public.returns WHERE id = %s"


async def get_return(conn: AsyncConnection, return_id: UUID) -> ReturnRow | None:
    """The return with this id, or None."""
    async with conn.cursor(row_factory=class_row(ReturnRow)) as cursor:
        await cursor.execute(RETURN_BY_ID_SQL, (return_id,))
        return await cursor.fetchone()
