"""The closed lists of the returns write side (ADR-0004, Decision 2; issue #54).

`TRANSIENT_ERROR_TYPES` is the single definition of "transient" for the retry rule: ADR-0004 lists
`llm_timeout`, `rate_limited`, `provider_unavailable`, `capacity` and `abandoned` as transient and
`invalid_llm_output` as permanent. Tests check these sets against `returns_error_type_ck` in
db/schema.sql and against the ADR, so a change here must change both.

The audit event types are one row per attempt (issue #54, decision 1). The list is closed in code,
not by a CHECK, so a later ticket (P1-13) can add to it without a migration.
"""

STATUSES: frozenset[str] = frozenset({"processing", "decided", "pending_review", "failed"})
OUTCOMES: frozenset[str] = frozenset({"approve", "deny", "escalate"})

TRANSIENT_ERROR_TYPES: frozenset[str] = frozenset(
    {"llm_timeout", "rate_limited", "provider_unavailable", "capacity", "abandoned"}
)
PERMANENT_ERROR_TYPES: frozenset[str] = frozenset({"invalid_llm_output"})
ERROR_TYPES: frozenset[str] = TRANSIENT_ERROR_TYPES | PERMANENT_ERROR_TYPES

# `superseded`: an attempt lost its compare-and-set and writes only this row (ADR-0004, Decision 5).
_FIXED_EVENT_TYPES: frozenset[str] = frozenset(
    {"rules_decided", "llm_decided", "escalated", "r4_denied", "superseded"}
)
EVENT_TYPES: frozenset[str] = _FIXED_EVENT_TYPES | {f"failed_{t}" for t in ERROR_TYPES}


def failed_event(error_type: str) -> str:
    """The audit event type of a failed attempt: `failed_<error_type>`.

    An error type outside ERROR_TYPES is a ValueError: no other value may reach the audit table,
    and the error type is kept only here (a retry clears `returns.error_type`).
    """
    if error_type not in ERROR_TYPES:
        raise ValueError(f"unknown error type: {error_type!r}")
    return f"failed_{error_type}"
