"""The constants of the write side (issue #54).

The two index names are module constants in app/db/returns.py, because the code needs them to tell
the two unique violations apart (`diag.constraint_name`). They are not retyped here: the expected
names are parsed from db/schema.sql, so a rename in a migration fails this test, not production.
"""

from repo_schema import schema_active_order_line_index, schema_key_index

from app.db.returns import ACTIVE_ORDER_LINE_INDEX, ACTIVE_RETURN_PREDICATE_SQL, KEY_INDEX


def test_the_key_index_is_the_one_in_the_schema():
    assert KEY_INDEX == schema_key_index()


def test_the_active_order_line_index_is_the_one_in_the_schema():
    assert ACTIVE_ORDER_LINE_INDEX == schema_active_order_line_index()


def test_the_two_names_are_different():
    assert schema_key_index() != schema_active_order_line_index()
    assert KEY_INDEX != ACTIVE_ORDER_LINE_INDEX


def test_the_active_predicate_is_not_empty():
    # What it must MEAN is checked against the real index by a matrix test on Postgres.
    assert ACTIVE_RETURN_PREDICATE_SQL.strip()
