"""The result classes of the write side (issue #54).

They are frozen pydantic models that forbid extra fields, one small class per outcome. The union
aliases (`InsertResult`, `KeyLookup`, `FinalizeResult`, `RetryResult`) list every member, and the
two classes that stand for "nothing to carry" (`Lost`, `KeyConflict`) have no fields. These checks
are structural: they pass on the stubs, because the classes already exist; what they pin is the
shape 12b will depend on.
"""

import typing

import pytest
from pydantic import ValidationError
from repo_unit_helpers import sample_row

from app.db import models
from app.db.models import (
    Applied,
    ExpiredKey,
    FinalizeResult,
    InFlight,
    Inserted,
    InsertResult,
    KeyConflict,
    KeyLookup,
    Lost,
    NoKey,
    PayloadMismatch,
    R4Denied,
    Replay,
    Retried,
    Retryable,
    RetryResult,
)

ROW = sample_row()

# (class, the keyword arguments that build one, its field names)
CASES = [
    (Inserted, {"row": ROW}, {"row"}),
    (R4Denied, {"row": ROW}, {"row"}),
    (KeyConflict, {}, set()),
    (NoKey, {}, {"next_generation"}),
    (ExpiredKey, {"row": ROW, "next_generation": 2}, {"row", "next_generation"}),
    (PayloadMismatch, {"row": ROW}, {"row"}),
    (InFlight, {"row": ROW}, {"row"}),
    (Retryable, {"row": ROW}, {"row"}),
    (Replay, {"row": ROW}, {"row"}),
    (Applied, {"row": ROW}, {"row"}),
    (Retried, {"row": ROW}, {"row"}),
    (Lost, {}, set()),
]
IDS = [case[0].__name__ for case in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "fields"), CASES, ids=IDS)
def test_each_class_is_frozen(cls, kwargs, fields):
    instance = cls(**kwargs)
    with pytest.raises(ValueError):  # pydantic's ValidationError is a ValueError
        instance.something = 1
    for name in fields:
        with pytest.raises(ValidationError):
            setattr(instance, name, kwargs.get(name, 1))


@pytest.mark.parametrize(("cls", "kwargs", "fields"), CASES, ids=IDS)
def test_each_class_forbids_extra_fields(cls, kwargs, fields):
    with pytest.raises(ValidationError):
        cls(**kwargs, bogus=1)


@pytest.mark.parametrize(("cls", "kwargs", "fields"), CASES, ids=IDS)
def test_each_class_has_exactly_its_fields(cls, kwargs, fields):
    assert set(cls.model_fields) == fields


def test_lost_and_key_conflict_carry_nothing():
    for cls in (Lost, KeyConflict):
        assert cls.model_fields == {}
        assert cls().model_dump() == {}
        assert cls() == cls()


def test_no_key_starts_at_generation_one():
    assert NoKey().next_generation == 1


def test_the_union_aliases_cover_every_member():
    assert set(typing.get_args(InsertResult)) == {Inserted, R4Denied, KeyConflict}
    assert set(typing.get_args(KeyLookup)) == {
        NoKey,
        ExpiredKey,
        PayloadMismatch,
        InFlight,
        Retryable,
        Replay,
    }
    assert set(typing.get_args(FinalizeResult)) == {Applied, Lost}
    assert set(typing.get_args(RetryResult)) == {Retried, R4Denied, Lost}


def test_every_result_class_of_the_module_is_in_a_case_above():
    # A class added to models.py later without a case here fails this test, so nobody forgets to
    # pin its shape.
    declared = {
        name
        for name, value in vars(models).items()
        if isinstance(value, type)
        and issubclass(value, models._Frozen)
        and value not in (models._Frozen, models.ReturnFields, models.DecisionRecord)
    }
    assert declared == {cls.__name__ for cls, _, _ in CASES}
