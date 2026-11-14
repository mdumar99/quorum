"""Baseline: no tables.

Creates nothing. It exists so the version table (schema `migrations`), the migrate step and the
drift check are exercised end to end before the schema ticket adds the first tables.

Forward-only (ADR-0002, Decision 1): there is no downgrade. A downgrade is a second code path
nothing in CI exercises, and for audit data an automatic "undo" would delete rows that policy
says are never deleted. A mistake is corrected by a new revision that moves forward.
"""

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass
