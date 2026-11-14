"""The database-roles doc names the real UPDATE grants (#44, criterion D3).

No database is needed: it reads docs/database-roles.md. It ships with the docs commit.
"""

from pathlib import Path

DOC = Path(__file__).resolve().parents[3] / "docs" / "database-roles.md"
REAL_GRANT = (
    "GRANT UPDATE (status, outcome, error_type, retry_after_seconds, attempt, updated_at)"
    " ON public.returns TO quorum_app"
)


def test_database_roles_doc_names_the_real_update_grants():
    text = DOC.read_text(encoding="utf-8")
    assert REAL_GRANT in text
    assert "GRANT UPDATE (status) ON public.returns" not in text, "the example grant is still there"
