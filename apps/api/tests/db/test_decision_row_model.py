"""DecisionRow is the database row model of `decisions`, not the API resource (issue #56).

It must match the table in db/schema.sql: the same columns in the same order, the same types, and
nullable exactly where the column is nullable (the ten model-call columns). The query lists the
columns explicitly (no `SELECT *`), so a schema change fails loudly instead of silently adding or
losing a field. `rule_results` is the JSON as stored (pydantic's JsonValue): the shape of the array
is checked by whoever reads it (issue #56 and 12b), because the schema has no CHECK on it.

The model carries no defaults: a row read from the table always has all 16 columns, so a missing
column is an error, nullable or not, as in ReturnRow.
"""

import re
from uuid import uuid4

import pytest
from db_helpers import decisions_columns
from pydantic import JsonValue, ValidationError

from app.db.audit import LLM_COLUMNS
from app.db.models import DecisionRow
from app.db.returns import DECISION_BY_RETURN_SQL, DECISION_COLUMNS

DECISION_COLUMN_NAMES = ("return_id", "policy_version", "outcome", "decided_by", "rule_results")


def sample_for(py_type: object) -> object:
    """A value of the column's Python type."""
    if py_type is JsonValue:
        return [{"rule_id": "R1", "passed": True, "details": {"days": 12, "note": None}}]
    if py_type == list[str]:
        return ["approve", "deny"]
    samples = {"UUID": uuid4(), "str": "text", "int": 7, "bool": True}
    return samples[py_type.__name__]


def sample_row() -> dict:
    """A value for every column, nullable ones included."""
    return {name: sample_for(py_type) for name, py_type, _ in decisions_columns()}


def test_the_schema_has_the_sixteen_columns_the_ticket_names():
    names = tuple(name for name, _, _ in decisions_columns())
    assert len(names) == 16
    assert names[:6] == (*DECISION_COLUMN_NAMES, "allowed_outcomes")
    assert names[6:] == tuple(LLM_COLUMNS)


def test_exactly_the_ten_model_call_columns_are_nullable():
    nullable = {name for name, _, is_nullable in decisions_columns() if is_nullable}
    assert nullable == set(LLM_COLUMNS)


def test_the_model_has_the_columns_of_the_table_in_the_same_order():
    assert tuple(DecisionRow.model_fields) == tuple(name for name, _, _ in decisions_columns())


@pytest.mark.parametrize("column", decisions_columns(), ids=lambda c: c[0])
def test_each_field_has_the_type_and_nullability_of_its_column(column):
    name, py_type, nullable = column
    assert name in DecisionRow.model_fields, f"the model has no field {name}"
    expected = (py_type | None) if nullable else py_type
    assert DecisionRow.model_fields[name].annotation == expected


def test_the_vocabulary_columns_are_plain_strings():
    """The database CHECKs own the vocabulary (outcome, decided_by); the version is free text."""
    for name in ("policy_version", "outcome", "decided_by"):
        assert name in DecisionRow.model_fields, f"the model has no field {name}"
        assert DecisionRow.model_fields[name].annotation is str


def test_a_full_row_builds_and_round_trips():
    values = sample_row()
    assert DecisionRow(**values).model_dump() == values


def test_a_row_with_every_nullable_column_empty_builds():
    """What a decision made by the rules looks like: the ten model-call columns are None."""
    values = {
        name: (None if nullable else sample_for(py_type))
        for name, py_type, nullable in decisions_columns()
    }
    assert DecisionRow(**values).model_dump() == values


def test_a_row_is_frozen():
    row = DecisionRow(**sample_row())
    with pytest.raises(ValidationError):
        row.outcome = "deny"


def test_an_extra_field_is_rejected():
    values = sample_row()
    DecisionRow(**values)  # the row itself is valid: only the extra field can be the reason
    with pytest.raises(ValidationError):
        DecisionRow(**values, unexpected=1)


@pytest.mark.parametrize("column", decisions_columns(), ids=lambda c: c[0])
def test_a_missing_column_is_rejected_nullable_or_not(column):
    values = sample_row()
    DecisionRow(**values)  # the full row is valid: only the missing column can be the reason
    del values[column[0]]
    with pytest.raises(ValidationError):
        DecisionRow(**values)


def test_a_value_of_the_wrong_type_is_rejected():
    values = sample_row()
    DecisionRow(**values)
    with pytest.raises(ValidationError):
        DecisionRow(**{**values, "latency_ms": "not a number"})


def test_rule_results_holds_any_json_as_stored():
    """The JSON as stored: this model does not decide what shape the array has."""
    values = sample_row()
    for stored in ([], [{"a": [1, 2, {"b": None}]}], {"not": "an array"}, "text", 3, None):
        assert DecisionRow(**{**values, "rule_results": stored}).rule_results == stored


def test_the_column_list_is_the_table():
    assert DECISION_COLUMNS == tuple(name for name, _, _ in decisions_columns())


def test_the_column_list_is_the_model_and_the_table():
    names = tuple(name for name, _, _ in decisions_columns())
    assert DECISION_COLUMNS == tuple(DecisionRow.model_fields) == names


def test_the_sql_selects_exactly_the_listed_columns_by_return_id():
    match = re.fullmatch(
        r"SELECT ([a-z_0-9]+(?:, [a-z_0-9]+)*) FROM public\.decisions WHERE return_id = %s",
        DECISION_BY_RETURN_SQL,
    )
    assert match is not None, "the SQL is not an explicit column list on public.decisions by id"
    assert tuple(match.group(1).split(", ")) == DECISION_COLUMNS
