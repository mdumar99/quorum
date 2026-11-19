"""Shared helpers for the unit tests of the returns repository (issue #54). No database.

The lists below are written out on purpose: they are the ADR-0004 and ADR-0003 facts that the code
is checked against, so they must not be derived from the code under test.
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID

from app.db.models import ReturnFields, ReturnRow
from app.llm.types import LLMCallRecord
from app.policy import PolicyCase, PolicyResult, evaluate, load_current_policy

# ADR-0004, Decision 2: "transient llm_timeout, rate_limited, provider_unavailable, capacity and
# abandoned; permanent invalid_llm_output".
ADR_TRANSIENT = frozenset(
    {"llm_timeout", "rate_limited", "provider_unavailable", "capacity", "abandoned"}
)
ADR_PERMANENT = frozenset({"invalid_llm_output"})
SIX_ERROR_TYPES = (
    "llm_timeout",
    "rate_limited",
    "provider_unavailable",
    "capacity",
    "abandoned",
    "invalid_llm_output",
)
TRANSIENT_FIVE = tuple(t for t in SIX_ERROR_TYPES if t != "invalid_llm_output")
# The event types of issue #54, decision 1.
STATIC_EVENT_TYPES = ("rules_decided", "llm_decided", "escalated", "r4_denied", "superseded")
EXPECTED_EVENT_TYPES = frozenset(STATIC_EVENT_TYPES) | {f"failed_{t}" for t in SIX_ERROR_TYPES}
ALL_EVENT_TYPES = tuple(sorted(EXPECTED_EVENT_TYPES))

POLICY_VERSION = "v1:" + "a" * 64
SAMPLE_ID = UUID("6f1d9a52-3c1e-4b7a-9d2e-0a1b2c3d4e5f")
AS_OF = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)


class TouchedConnection:
    """A stand-in for a database connection that fails the moment anything touches it.

    Passing it proves that a function refused its input BEFORE any SQL ran. A function that does
    reach the database fails with this message, which is how a test shows that a valid input is
    NOT refused early.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"the connection was touched (attribute {name!r})")


def run(coroutine):
    """Run one coroutine from a sync test (no pytest-asyncio: that would be a new dependency)."""
    return asyncio.run(coroutine)


def raw_fields(**overrides) -> dict:
    """The seven request fields as the JSON of a request holds them (the date is a string)."""
    values = {
        "order_id": "order-1001",
        "order_line_id": "line-1",
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "USD",
        "delivered_on": "2026-11-01",
        "reason": "does not fit",
    }
    values.update(overrides)
    return values


def base_fields(**overrides) -> ReturnFields:
    return ReturnFields(**raw_fields(**overrides))


def sample_row(**overrides) -> ReturnRow:
    """A valid `processing` row with every one of the 20 columns."""
    values = {
        "id": SAMPLE_ID,
        "order_id": "order-1001",
        "order_line_id": "line-1",
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "USD",
        "delivered_on": date(2026, 11, 1),
        "reason": "does not fit",
        "status": "processing",
        "outcome": None,
        "error_type": None,
        "retry_after_seconds": None,
        "attempt": 1,
        "as_of": AS_OF,
        "policy_version": POLICY_VERSION,
        "idempotency_key": "key-1",
        "payload_hash": "0" * 64,
        "key_generation": 1,
        "created_at": AS_OF,
        "updated_at": AS_OF,
    }
    values.update(overrides)
    return ReturnRow(**values)


def policy_result(category: str = "shoes") -> PolicyResult:
    """A real PolicyResult from the real v1 policy: `shoes` passes every rule, `digital` is hard
    denied (R2), so its allowed_outcomes is deny only."""
    case = PolicyCase(
        category=category, amount_minor=1999, currency="USD", delivered_on=date(2026, 11, 1)
    )
    return evaluate(load_current_policy(), case, AS_OF)


def llm_record() -> LLMCallRecord:
    return LLMCallRecord(provider="fake", model_id="fake-v1", prompt_version="pv:1", latency_ms=5)
