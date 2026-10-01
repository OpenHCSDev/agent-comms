from dataclasses import dataclass, field
from typing import Annotated, ClassVar

import pytest

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText, TimestampText, TextRepresentation, WireValue


@dataclass(frozen=True)
class OwnedWireText(WireValue):
    value: str

    def to_wire(self):
        return self.value

    @classmethod
    def from_wire(cls, data):
        return cls(FieldCodec.decode(str, data))

    @classmethod
    def schema(cls):
        return FieldCodec.value_schema(str)


@dataclass(frozen=True)
class OwnedWireRecord:
    value: OwnedWireText | None
    nested: tuple[OwnedWireText, ...]


def test_new_value_owns_wire_form_everywhere_without_a_codec_adapter():
    value = OwnedWireRecord(OwnedWireText("retained"), (OwnedWireText("nested"),))
    encoded = {"value": "retained", "nested": ["nested"]}
    assert FieldCodec.encode(value) == encoded
    assert FieldCodec.decode(OwnedWireRecord, encoded) == value
    assert FieldCodec.project(value, "status") == encoded
    assert FieldCodec.value_schema(OwnedWireText) == {"type": "string"}
    assert FieldCodec.decode(OwnedWireRecord, {**encoded, "value": None}).value is None
    with pytest.raises(ValueError):
        FieldCodec.decode(OwnedWireRecord, {**encoded, "value": 42})
    with pytest.raises(TypeError):
        FieldCodec.decode(OwnedWireText, object())


def test_declared_scalar_capabilities_and_new_case_use_the_same_boundary():
    from datetime import UTC, datetime
    from pathlib import Path
    from agent_comms.typed_table import Column, TextStorage, TypedTable
    import sqlite3

    class HexInteger(TextRepresentation):
        @classmethod
        def encode(cls, value):
            if type(value) is not int:
                raise TypeError("Expected an integer")
            return hex(value)

        @classmethod
        def from_text(cls, value):
            return int(value, 16)

    @dataclass(frozen=True)
    class Scalars:
        path: Annotated[Path | None, PathText]
        timestamp: Annotated[datetime, TimestampText]
        count: Annotated[int, HexInteger]

    value = Scalars(Path("/saved/history"), datetime.now(UTC), 31)
    encoded = FieldCodec.encode(value)
    assert encoded == {"path": "/saved/history", "timestamp": value.timestamp.isoformat(), "count": "0x1f"}
    assert FieldCodec.decode(Scalars, encoded) == value
    assert FieldCodec.project(value, "status") == encoded
    assert FieldCodec.value_schema(Annotated[Path | None, PathText]) == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    assert FieldCodec.decode(Scalars, {**encoded, "path": None}).path is None
    for key in encoded:
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(Scalars, {**encoded, key: 7})
    with pytest.raises(TypeError):
        FieldCodec.encode(value.timestamp)

    @dataclass(frozen=True)
    class ScalarRow(TypedTable, declared_name="field_representation_rows"):
        count: Annotated[int, HexInteger] = field(metadata={"sql": Column(storage=TextStorage)})

    with sqlite3.connect(":memory:") as db:
        ScalarRow.create(db)
        ScalarRow(31).insert(db)
        assert db.execute("SELECT count FROM field_representation_rows").fetchone() == ("0x1f",)
        assert ScalarRow.read(db.execute("SELECT count FROM field_representation_rows")) == [ScalarRow(31)]


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


def test_reserved_family_tag_cannot_be_shadowed_by_a_field():
    class Local(DeclaredFamily):
        pass

    @dataclass
    class Invalid(Local):
        kind: str

    with pytest.raises(TypeError, match="Conflicting"):
        FieldCodec.encode(Invalid("shadow"))


def test_defaults_nested_optional_and_nonfinite_decode():
    from dataclasses import replace

    record = Record(CountChoice(4), ("a", "b"), {}, optional=2.5)
    assert FieldCodec.decode(Record, FieldCodec.encode(record)) == record
    assert (
        FieldCodec.decode(Record, FieldCodec.encode(replace(record, optional=None))).optional
        is None
    )
    with pytest.raises(ValueError):
        FieldCodec.decode(float, float("nan"))


@dataclass(frozen=True)
class ChoiceReference:
    choice: type[Choice]


def test_nominal_class_reference_retains_owner_without_inventing_state_payload():
    record = ChoiceReference(CountChoice)
    assert FieldCodec.encode(record) == {"choice": "count"}
    assert FieldCodec.decode(ChoiceReference, {"choice": "count"}) == record
    # References do not fabricate CountChoice.value; instances still require it.
    with pytest.raises(TypeError):
        FieldCodec.decode(Choice, {"kind": "count"})


@pytest.mark.parametrize("value", ["unknown", 1, True, None, {}, {"kind": "count"}])
def test_nominal_class_reference_rejects_unknown_names_and_wrong_shapes(value):
    with pytest.raises(ValueError):
        FieldCodec.decode(ChoiceReference, {"choice": value})


def test_request_schema_preserves_nullable_fields_and_owned_nonnull_constraint():
    @dataclass(frozen=True)
    class Request:
        nullable: str | None = None
        nonnull: str | None = field(default=None, metadata={"wire_nonnull": True})

    properties = FieldCodec.record_schema(Request)["properties"]
    assert properties["nullable"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert properties["nonnull"] == {"type": "string"}


def test_family_instance_schema_tracks_decode_members_and_custom_discriminator():
    class Scope(DeclaredFamily, affix="Scope"):
        family_discriminator = "selection"

    @dataclass(frozen=True)
    class CurrentScope(Scope):
        pass

    @dataclass(frozen=True)
    class ExplicitScope(Scope):
        project: str = field(metadata={"wire_name": "externalProject"})

    @dataclass(frozen=True)
    class Request:
        scope: Scope

    def alternatives():
        return FieldCodec.record_schema(Request)["properties"]["scope"]["oneOf"]

    for value in (CurrentScope(), ExplicitScope("/project")):
        encoded = FieldCodec.encode(value)
        schema, = [item for item in alternatives()
                   if item["properties"]["selection"]["const"] == encoded["selection"]]
        assert set(schema["required"]) <= encoded.keys()
        assert encoded.keys() <= schema["properties"].keys()
        assert schema["additionalProperties"] is False
        assert FieldCodec.decode(Scope, encoded) == value
        assert FieldCodec.decode(Request, {"scope": encoded}) == Request(value)

    assert FieldCodec.value_schema(type[Scope])["enum"] == list(Scope.names())

    @dataclass(frozen=True)
    class NewScope(ExplicitScope):
        reason: str

    new = NewScope("/project", "new declaration")
    encoded = FieldCodec.encode(new)
    schema, = [item for item in alternatives()
               if item["properties"]["selection"]["const"] == encoded["selection"]]
    assert set(schema["required"]) == {"selection", "externalProject", "reason"}
    assert FieldCodec.decode(Scope, encoded) == new
    assert len(FieldCodec.value_schema(ExplicitScope)["oneOf"]) == 2


def test_uninhabited_family_schema_rejects_every_value_without_a_manual_roster():
    class Empty(DeclaredFamily, affix="Empty"):
        pass

    assert FieldCodec.value_schema(Empty) == {"not": {}}
    with pytest.raises(ValueError, match="Unknown"):
        FieldCodec.decode(Empty, {"kind": "unregistered"})
