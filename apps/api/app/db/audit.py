"""The audit row of an attempt (ADR-0003, Decision 10; ADR-0004, Decision 5; issue #54).

One row per attempt, written in the caller's transaction, next to the status change it describes.
`event_type` is one of the closed list in `app.db.vocabulary`; a type outside it is a ValueError,
raised BEFORE any SQL, so the table never holds a value the code does not know.
"""

from uuid import UUID

from psycopg import AsyncConnection

from app.db.vocabulary import EVENT_TYPES
from app.llm.types import LLMCallRecord

# The ten LLM columns of audit_log (and of decisions), in the order LLMCallRecord.columns() has.
LLM_COLUMNS: tuple[str, ...] = (
    "provider",
    "model_id",
    "prompt_version",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "raw_output",
    "raw_output_bytes",
    "raw_output_sha256",
    "raw_output_truncated",
)

AUDIT_INSERT_SQL = (
    "INSERT INTO public.audit_log (return_id, attempt, event_type, "
    + ", ".join(LLM_COLUMNS)
    + ", conflicting_return_id) VALUES (%(return_id)s, %(attempt)s, %(event_type)s, "
    + ", ".join(f"%({column})s" for column in LLM_COLUMNS)
    + ", %(conflicting_return_id)s) RETURNING id"
)


def llm_values(llm: LLMCallRecord | None) -> dict[str, object]:
    """The ten LLM column values of a call, or all NULL when there was no call."""
    return llm.columns() if llm is not None else dict.fromkeys(LLM_COLUMNS)


async def insert_audit(
    conn: AsyncConnection,
    return_id: UUID,
    attempt: int,
    event_type: str,
    llm: LLMCallRecord | None = None,
    conflicting_return_id: UUID | None = None,
) -> int:
    """Insert one audit row and return its id.

    An event type outside EVENT_TYPES is a ValueError, raised before the connection is touched.
    `llm` is the model call of this attempt, kept also when the call failed; `conflicting_return_id`
    is set only for an R4 denial. The caller owns the transaction: nothing is committed here.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown audit event type: {event_type!r}")
    params = {
        "return_id": return_id,
        "attempt": attempt,
        "event_type": event_type,
        "conflicting_return_id": conflicting_return_id,
        **llm_values(llm),
    }
    cursor = await conn.execute(AUDIT_INSERT_SQL, params)
    (audit_id,) = await cursor.fetchone()
    return audit_id
