"""Facts about `returns` read from db/schema.sql for the repository tests (issue #54).

Nothing here is retyped from the schema: every list and name is parsed from the committed file. A
parse that finds nothing is an assertion failure, never an empty list (an empty list would let a
test that compares against it pass for the wrong reason).
"""

import re

from db_helpers import SCHEMA


def _schema_text() -> str:
    assert SCHEMA.is_file(), "db/schema.sql does not exist"
    return SCHEMA.read_text()


def _check_values(constraint: str) -> frozenset[str]:
    """The quoted text values of one single-line CHECK of public.returns."""
    for line in _schema_text().splitlines():
        if f"CONSTRAINT {constraint} CHECK" in line:
            values = frozenset(re.findall(r"'([a-z_]+)'::text", line))
            assert values, f"{constraint} lists no text values"
            return values
    raise AssertionError(f"{constraint} not found in db/schema.sql")


def schema_error_types() -> frozenset[str]:
    return _check_values("returns_error_type_ck")


def schema_statuses() -> frozenset[str]:
    return _check_values("returns_status_ck")


def schema_outcomes() -> frozenset[str]:
    return _check_values("returns_outcome_ck")


def _index_name(columns: str) -> str:
    pattern = rf"CREATE UNIQUE INDEX (\w+) ON public\.returns USING btree \({columns}\)"
    matches = re.findall(pattern, _schema_text())
    assert len(matches) == 1, f"expected one unique index on ({columns}), found {len(matches)}"
    return matches[0]


def schema_active_order_line_index() -> str:
    """The partial unique index on (order_id, order_line_id): R4 (ADR-0004, Decision 6)."""
    name = _index_name("order_id, order_line_id")
    assert re.search(rf"CREATE UNIQUE INDEX {name} .*\) WHERE ", _schema_text()), (
        f"{name} is not a partial index"
    )
    return name


def schema_key_index() -> str:
    """The unique index on (idempotency_key, key_generation)."""
    return _index_name("idempotency_key, key_generation")
