"""Database row models (issue #50).

`ReturnRow` is the row of the `returns` table, NOT the API resource: it has columns the API never
returns (idempotency_key, payload_hash, key_generation). It has the 20 columns of db/schema.sql
in table order, with their types, and is nullable exactly where the column is.

status, outcome and error_type are plain `str`: the database CHECKs own that vocabulary, and P1-12
maps rows to the API's types. A test compares this model with the table in db/schema.sql.
"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.llm.types import LLMCallRecord
from app.policy import PolicyResult


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


# ---------- the write side (issue #54) ----------


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReturnFields(_Frozen):
    """The seven validated request fields (ADR-0004, Decision 1), as they are stored and hashed."""

    order_id: str
    order_line_id: str
    category: str
    amount_minor: int
    currency: str
    delivered_on: date
    reason: str


class DecisionRecord(_Frozen):
    """What a decision row needs: the outcome, who decided, the policy result and the model call.

    Two invariants, the ones the schema enforces on `decisions` (`decisions_llm_fields_ck` and
    `decisions_outcome_ck`), checked here so that a bad record never reaches the database: the
    model call is present if and only if `decided_by` is "llm", and the outcome is one of the
    policy result's `allowed_outcomes`.
    """

    outcome: Literal["approve", "deny", "escalate"]
    decided_by: Literal["rules", "llm"]
    policy_result: PolicyResult
    llm: LLMCallRecord | None = None

    @model_validator(mode="after")
    def _the_record_agrees_with_itself(self) -> "DecisionRecord":
        if (self.decided_by == "llm") != (self.llm is not None):
            raise ValueError("a model call is present if and only if decided_by is 'llm'")
        if self.outcome not in self.policy_result.allowed_outcomes:
            raise ValueError("the outcome is not one of the allowed outcomes of the policy result")
        return self


# insert_return -> Inserted | R4Denied | KeyConflict
class Inserted(_Frozen):
    row: ReturnRow


class R4Denied(_Frozen):
    row: ReturnRow


class KeyConflict(_Frozen):
    """The (key, generation) already exists: a same-key race, never R4."""


# lookup_key -> NoKey | ExpiredKey | PayloadMismatch | InFlight | Retryable | Replay
class NoKey(_Frozen):
    next_generation: int = 1


class ExpiredKey(_Frozen):
    row: ReturnRow
    next_generation: int


class PayloadMismatch(_Frozen):
    row: ReturnRow


class InFlight(_Frozen):
    row: ReturnRow


class Retryable(_Frozen):
    row: ReturnRow


class Replay(_Frozen):
    row: ReturnRow


# finalize_decided, finalize_failed -> Applied | Lost; retry_failed -> Retried | R4Denied | Lost
class Applied(_Frozen):
    row: ReturnRow


class Retried(_Frozen):
    row: ReturnRow


class Lost(_Frozen):
    """The compare-and-set changed 0 rows: another attempt owns the row."""


InsertResult = Inserted | R4Denied | KeyConflict
KeyLookup = NoKey | ExpiredKey | PayloadMismatch | InFlight | Retryable | Replay
FinalizeResult = Applied | Lost
RetryResult = Retried | R4Denied | Lost
