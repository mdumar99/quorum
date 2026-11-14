"""Policy v1: a versioned parameter file, the pure rules R1 to R3 and the PolicyResult contract.

ADR-0003, Decision 2. See docs/policy.md for how to add a version and for the JSON shape.
"""

from app.policy.loader import (
    CURRENT_POLICY_NAME,
    load_current_policy,
    load_policy,
)
from app.policy.models import (
    Policy,
    PolicyCase,
    PolicyError,
    PolicyHashMismatchError,
    PolicyNotFoundError,
    PolicyResult,
    RuleResult,
)
from app.policy.rules import evaluate

__all__ = [
    "CURRENT_POLICY_NAME",
    "Policy",
    "PolicyCase",
    "PolicyError",
    "PolicyHashMismatchError",
    "PolicyNotFoundError",
    "PolicyResult",
    "RuleResult",
    "evaluate",
    "load_current_policy",
    "load_policy",
]
