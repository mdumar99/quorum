"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Hand-written: write the SQL yourself (`from alembic import op`, then `op.execute(...)`). Grants
and default privileges are part of every schema change (ADR-0002, Decision 3).

Forward-only (ADR-0002, Decision 1): there is no downgrade. A downgrade is a second code path
nothing in CI exercises, and for audit data an automatic "undo" would delete rows that policy
says are never deleted. A mistake is corrected by a new revision that moves forward.

The history is linear, with one parent per revision; merge revisions are not supported by this
template.
"""

revision: str = "${up_revision}"
down_revision: str | None = ${"None" if down_revision is None else '"%s"' % down_revision}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    pass
