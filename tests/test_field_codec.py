from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec


class Choice(DeclaredFamily, affix="Choice"):
    pass


@dataclass(frozen=True, slots=True)
class CountChoice(Choice):
    value: int


@dataclass(frozen=True)
class Record:
    choice: Choice
    pair: tuple[str, str]
    aliases: dict[str, list[int]]
    label: str = field(default="default", metadata={"wire_name": "externalLabel"})
    optional: float | None = None
    ignored: ClassVar[str] = "not stored"


def test_golden_nested_family_record_round_trip():
    record = Record(CountChoice(4), ("alice", "bob"), {"a": [1, 2]})
    golden = {
        "choice": {"kind": "count", "value": 4},
        "pair": ["alice", "bob"],
        "aliases": {"a": [1, 2]},
        "externalLabel": "default",
        "optional": None,
    }
    assert FieldCodec.encode(record) == golden
    assert FieldCodec.decode(Record, golden) == record
    del golden["externalLabel"]
    assert FieldCodec.decode(Record, golden) == record


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "count", "value": True},
        {"kind": "count", "value": "1"},
        {"kind": "unknown", "value": 1},
        {"kind": "count", "value": 1, "extra": 2},
        {"kind": "count"},
        {},
        [],
    ],
)
def test_invalid_family_boundaries(data):
    with pytest.raises((ValueError, TypeError)):
        FieldCodec.decode(Choice, data)


def test_alias_collision_and_reserved_tag():
    @dataclass
    class Bad:
        first: str
        second: str = field(metadata={"wire_name": "first"})

    with pytest.raises(TypeError, match="Conflicting"):
        FieldCodec.encode(Bad("a", "b"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {1: "x"}])
def test_non_json_values_rejected(value):
    with pytest.raises(TypeError):
        FieldCodec.encode(value)


def test_tuple_shape_and_strict_bool():
    with pytest.raises(ValueError):
        FieldCodec.decode(tuple[str, str], ["one"])
    with pytest.raises(ValueError):
        FieldCodec.decode(bool, 1)
