"""Database row models (issue #50).

`ReturnRow` is the row of the `returns` table, NOT the API resource: it has columns the API never
returns (idempotency_key, payload_hash, key_generation). It has the 20 columns of db/schema.sql
in table order, with their types, and is nullable exactly where the column is.

status, outcome and error_type are plain `str`: the database CHECKs own that vocabulary, and P1-12
maps rows to the API's types. A test compares this model with the table in db/schema.sql.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ReturnRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    order_id: str
    order_line_id: str
    category: str
    amount_minor: int
    currency: str
    delivered_on: date
    reason: str
    status: str
    outcome: str | None
    error_type: str | None
    retry_after_seconds: int | None
    attempt: int
    as_of: datetime
    policy_version: str
    idempotency_key: str
    payload_hash: str
    key_generation: int
    created_at: datetime
    updated_at: datetime
