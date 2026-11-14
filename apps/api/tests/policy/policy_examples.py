"""Builders, pinned values and the two golden examples for the policy tests of #46 (not tests).

The golden examples are the PolicyResult JSON form approved on #46. The shape test and the doc
test compare against these same objects, so the doc cannot drift from the code.

The canonical hash helper below follows the approved serialization (sorted keys, separators
",", ":", ASCII, excluded categories sorted). It is written here on purpose, apart from the
implementation, so a test can check the implementation against it.
"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# The database time of the examples: 2026-11-14 at 09:30 UTC.
AS_OF = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)

# The canonical string of policy v1 and its SHA-256, as approved on #46. The test that pins them
# also checks that the hash is the hash of the string.
V1_CANONICAL = (
    '{"rules":{"R1":{"window_days":30},"R2":{"excluded_categories":["digital","final_sale"]},'
    '"R3":{"ceiling_minor":20000,"currency":"USD"}},"version":"v1"}'
)
V1_HASH = "8b7bd6fa56e53a2fb799f5fb60855bde004a6b6adb6ac30c70667c7f9abc324c"
V1_POLICY_VERSION = f"v1:{V1_HASH}"


def toml_from(
    name: str = "v1",
    window_days: int = 30,
    categories: tuple[str, ...] = ("final_sale", "digital"),
    ceiling: int = 20000,
    currency: str = "USD",
) -> str:
    """The text of a policy file with these parameters (the line formats are fixed on purpose)."""
    listed = ", ".join(f'"{category}"' for category in categories)
    return (
        f'version = "{name}"\n'
        "\n"
        "[rules.R1]\n"
        f"window_days = {window_days}\n"
        "\n"
        "[rules.R2]\n"
        f"excluded_categories = [{listed}]\n"
        "\n"
        "[rules.R3]\n"
        f"ceiling_minor = {ceiling}\n"
        f'currency = "{currency}"\n'
    )


def doc_from(**overrides) -> dict:
    """The validated content of the same file, as the canonical document is built from."""
    params = {
        "name": "v1",
        "window_days": 30,
        "categories": ("final_sale", "digital"),
        "ceiling": 20000,
        "currency": "USD",
    }
    params.update(overrides)
    return {
        "version": params["name"],
        "rules": {
            "R1": {"window_days": params["window_days"]},
            "R2": {"excluded_categories": sorted(params["categories"])},
            "R3": {"ceiling_minor": params["ceiling"], "currency": params["currency"]},
        },
    }


def canonical_hash(doc: dict) -> str:
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The text of policy v1 as the tests write it into fixture directories.
V1_TEXT = toml_from()


def write_version(directory: Path, name: str, text: str) -> Path:
    """Write a policy file as bytes (so CRLF stays CRLF) into a fixture directory."""
    path = directory / f"{name}.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def make_case(**overrides):
    """A valid case (shoes, 1999 USD, delivered 2026-11-01); any field can be overridden."""
    from app.policy import PolicyCase

    values = {
        "category": "shoes",
        "amount_minor": 1999,
        "currency": "USD",
        "delivered_on": date(2026, 11, 1),
    }
    values.update(overrides)
    return PolicyCase(**values)


def run(policy, as_of: datetime = AS_OF, **case_overrides):
    """Evaluate a case built with make_case against AS_OF (or another aware datetime)."""
    from app.policy import evaluate

    return evaluate(policy, make_case(**case_overrides), as_of)


def rule(result, rule_id: str):
    """The rule result with this id."""
    return next(entry for entry in result.rule_results if entry.rule_id == rule_id)


def delivered(days_before: int, as_of: datetime = AS_OF) -> date:
    """The delivery date that is this many days before the UTC date of as_of."""
    return as_of.astimezone(UTC).date() - timedelta(days=days_before)


R3_PASS = {
    "rule_id": "R3",
    "passed": True,
    "effect": "limit_outcomes",
    "reason_code": "within_ceiling",
    "details": {
        "amount_minor": 1999,
        "ceiling_minor": 20000,
        "currency": "USD",
        "currency_matches": True,
    },
}

# Example 1 of #46: all rules pass (shoes, 1999 USD, delivered 2026-11-01).
EXAMPLE_ALL_PASS = {
    "policy_version": V1_POLICY_VERSION,
    "hard_deny": False,
    "allowed_outcomes": ["approve", "deny", "escalate"],
    "rule_results": [
        {
            "rule_id": "R1",
            "passed": True,
            "effect": "hard_deny",
            "reason_code": "within_window",
            "details": {
                "delivered_on": "2026-11-01",
                "as_of_date_utc": "2026-11-14",
                "days_since_delivery": 13,
                "window_days": 30,
            },
        },
        {
            "rule_id": "R2",
            "passed": True,
            "effect": "hard_deny",
            "reason_code": "category_allowed",
            "details": {"category_excluded": False, "matched_category": None},
        },
        R3_PASS,
    ],
}

# Example 2 of #46: R1 and R2 fail together (digital, delivered 2026-09-01, 1999 USD); R3 passes.
EXAMPLE_R1_R2_FAIL = {
    "policy_version": V1_POLICY_VERSION,
    "hard_deny": True,
    "allowed_outcomes": ["deny"],
    "rule_results": [
        {
            "rule_id": "R1",
            "passed": False,
            "effect": "hard_deny",
            "reason_code": "outside_window",
            "details": {
                "delivered_on": "2026-09-01",
                "as_of_date_utc": "2026-11-14",
                "days_since_delivery": 74,
                "window_days": 30,
            },
        },
        {
            "rule_id": "R2",
            "passed": False,
            "effect": "hard_deny",
            "reason_code": "category_excluded",
            "details": {"category_excluded": True, "matched_category": "digital"},
        },
        R3_PASS,
    ],
}

# The entry P1-12 writes for an R4 loser, as a one-entry array (R4 fires before R1 to R3 run).
R4_ENTRY = {
    "rule_id": "R4",
    "passed": False,
    "effect": "hard_deny",
    "reason_code": "active_return_exists",
    "details": {},
}
