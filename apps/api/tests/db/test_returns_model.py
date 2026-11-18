"""ReturnRow is the database row model of `returns`, not the API resource (issue #50).

It must match the table in db/schema.sql: the same columns in the same order, the same types, and
nullable exactly where the column is nullable. The query lists the columns explicitly (no
`SELECT *`), so a schema change fails loudly instead of silently adding or losing a field.
"""

import re
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from db_helpers import returns_columns
from pydantic import ValidationError

from app.db.models import ReturnRow
from app.db.returns import RETURN_BY_ID_SQL, RETURN_COLUMNS

SAMPLES = {
    "uuid": uuid4(),
    "str": "text",
    "int": 7,
    "date": date(2026, 11, 1),
    "datetime": datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
}


def sample_row() -> dict:
    """A value for every column, nullable ones included."""
    return {name: SAMPLES[py_type.__name__.lower()] for name, py_type, _ in returns_columns()}


def test_the_model_has_the_columns_of_the_table_in_the_same_order():
    assert tuple(ReturnRow.model_fields) == tuple(name for name, _, _ in returns_columns())


@pytest.mark.parametrize("column", returns_columns(), ids=lambda c: c[0])
def test_each_field_has_the_type_and_nullability_of_its_column(column):
    name, py_type, nullable = column
    expected = (py_type | None) if nullable else py_type
    assert ReturnRow.model_fields[name].annotation == expected


def test_the_vocabulary_columns_are_plain_strings():
    """The database CHECKs own the vocabulary; P1-12 maps rows to the API's types."""
    for name in ("status", "outcome", "error_type"):
        assert ReturnRow.model_fields[name].annotation in (str, str | None)


def test_a_full_row_builds_and_round_trips():
    values = sample_row()
    assert ReturnRow(**values).model_dump() == values


def test_a_row_is_frozen():
    row = ReturnRow(**sample_row())
    with pytest.raises(ValidationError):
        row.status = "failed"


def test_an_extra_field_is_rejected():
    with pytest.raises(ValidationError):
        ReturnRow(**sample_row(), unexpected=1)


def test_a_missing_column_is_rejected():
    values = sample_row()
    del values["payload_hash"]
    with pytest.raises(ValidationError):
        ReturnRow(**values)


def test_a_value_of_the_wrong_type_is_rejected():
    with pytest.raises(ValidationError):
        ReturnRow(**{**sample_row(), "amount_minor": "not a number"})


def test_the_column_list_is_the_model_and_the_table():
    names = tuple(name for name, _, _ in returns_columns())
    assert RETURN_COLUMNS == tuple(ReturnRow.model_fields) == names


def test_the_sql_selects_exactly_the_listed_columns_by_id():
    match = re.fullmatch(
        r"SELECT ([a-z_]+(?:, [a-z_]+)*) FROM public\.returns WHERE id = %s", RETURN_BY_ID_SQL
    )
    assert match is not None, "the SQL is not an explicit column list on public.returns by id"
    assert tuple(match.group(1).split(", ")) == RETURN_COLUMNS
