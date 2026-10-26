"""Observation identity follows comparison declarations, not presentation fields."""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Annotated

import pytest

from agent_comms.field_codec import FieldCodec, WireRequired
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_creation import CreationSeconds, CreationTime, RecordedCreationTime
from agent_comms.thread_identity import RecordedThreadIncarnation


@dataclass(frozen=True)
class Observation:
    owner: RecordedThreadIncarnation
    revision: int
    reader: object = field(compare=False, repr=False)


@pytest.mark.parametrize("wrap", (
    lambda value: (value,),
    lambda value: [value],
    lambda value: frozenset((value,)),
    lambda value: {"observation": value},
))
def test_comparison_recurses_through_container_and_record_declarations(wrap):
    original = Observation(RecordedThreadIncarnation("before", 123.0), 1, object())
    renamed = Observation(RecordedThreadIncarnation("after", 123.0), 1, object())
    changed = Observation(RecordedThreadIncarnation("after", 123.0), 2, object())
    assert original == renamed and original != changed
    before, after, revised = (FieldCodec.comparison_digest(wrap(value))
                              for value in (original, renamed, changed))
    assert before == after and before != revised


class Position(IntEnum):
    FIRST = 1


@pytest.mark.parametrize("value", (Position.FIRST, 1, True, "value", Path("nested/path")))
def test_inherited_origin_does_not_replace_exact_scalar_or_enum_declarations(value):
    encoded = FieldCodec.encode(value, type(value))
    assert FieldCodec.decode(type(value), encoded) == value


def test_unordered_comparison_is_independent_of_insertion_order():
    left = {"first": frozenset(("b", "a")), "second": 2}
    right = {"second": 2, "first": frozenset(("a", "b"))}
    assert FieldCodec.comparison_digest(left) == FieldCodec.comparison_digest(right)


@dataclass(frozen=True)
class Born:
    created_at: Annotated[CreationTime, CreationSeconds, WireRequired()]


def test_represented_scalar_compares_through_its_declared_representation():
    recorded = Born(RecordedCreationTime(12.5))
    digest = FieldCodec.comparison_digest(recorded)
    assert digest == FieldCodec.comparison_digest(Born(RecordedCreationTime(12.5)))
    assert digest != FieldCodec.comparison_digest(Born(RecordedCreationTime(13.0)))


class Unowned:
    """A compared type the codec has no owner for."""


@dataclass(frozen=True)
class HoldsUnowned:
    label: str
    unowned: Unowned


def test_comparison_compiles_every_compared_field_and_refuses_an_unowned_type():
    with pytest.raises(TypeError, match="Unowned"):
        FieldCodec.compile_comparison(HoldsUnowned)
    assert StandingRule.names()  # discovery compiles every rule's comparison
