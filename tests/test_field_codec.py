import json

from conftest import transcript_records
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, ClassVar

import pytest

from agent_comms.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.field_codec import (
    FieldCodec,
    InlineRecord,
    JsonShapeFamily,
    JsonShapeMember,
    MalformedRecord,
    NullMember,
    PathText,
    RecordedTimestamp,
    SubclassUnion,
    TextRepresentation,
    TimestampText,
    WireValue,
    FlagDirective,
    WireAbsentNone,
    WireContext,
    WireInline,
    WireName,
    WireNonNull,
    WireTransient,
)
from agent_comms.messages import (
    ConversationRole,
    InfoKind,
    MembershipChange,
    MembershipConversationRole,
    MembershipNoticeRole,
    Message,
    MessageRole,
    NoticeRole,
)


def test_transient_plan_excludes_fields_before_decoder_or_value_access():
    class Opaque:
        pass

    @dataclass
    class Record:
        value: str
        local: Annotated[
            Opaque, WireTransient(), WireName("")
        ] = field(default_factory=Opaque, repr=False, compare=False)

        def __getattribute__(self, name):
            if name == "local":
                raise AssertionError("A wire operation read process-local state")
            return object.__getattribute__(self, name)

    record = Record("kept")
    assert [key for _, key in FieldCodec.record_fields(Record)] == ["value"]
    assert FieldCodec.encode(record) == {"value": "kept"}
    assert FieldCodec.project(record, "status") == {"value": "kept"}
    assert FieldCodec.decode(Record, {"value": "kept"}).value == "kept"
    assert set(FieldCodec.value_schema(Record)["properties"]) == {"value"}
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Record, {"value": "kept", "local": {}})


def test_record_exclusion_uses_the_selected_plan_and_refuses_conflicting_claims():
    @dataclass
    class Record:
        value: Annotated[str, WireTransient(), WireContext()]

    with pytest.raises(TypeError):
        FieldCodec.record_fields(Record)


def test_field_plan_discovers_a_new_flag_without_changing_the_selector():
    from agent_comms.field_codec import DeclaredFieldPlan

    class ProbeDirective(FlagDirective):
        pass

    class ProbePlan(DeclaredFieldPlan):
        directives = (ProbeDirective,)

        def omitted(self, value):
            return value == "omit" or super().omitted(value)

    @dataclass
    class Record:
        value: Annotated[str, ProbeDirective()]

    assert isinstance(FieldCodec.field_plan(Record, "value"), ProbePlan)
    assert FieldCodec.encode(Record("omit")) == {}
    assert FieldCodec.encode(Record("kept")) == {"value": "kept"}
    assert FieldCodec.decode(Record, {"value": "kept"}) == Record("kept")


def test_field_plan_refuses_an_unclaimed_representation():
    from agent_comms.field_codec import FieldPlan

    class UnclaimedPath(PathText):
        field_plan = FieldPlan  # The base has no registered field claim.

    @dataclass
    class Record:
        value: Annotated[Path, UnclaimedPath]

    with pytest.raises(TypeError):
        FieldCodec.field_plan(Record, "value")


def test_field_plan_refuses_unrelated_claims_instead_of_ordering_flags():
    @dataclass
    class Record:
        value: Annotated[
            str | None, WireContext(), WireAbsentNone()
        ]

    with pytest.raises(TypeError):
        FieldCodec.field_plan(Record, "value")


def test_wire_absence_is_none_without_running_the_constructor_factory():
    created = []

    def fresh():
        created.append("created")
        return "constructed-id"

    @dataclass
    class Record:
        id: Annotated[str | None, WireAbsentNone()] = field(
            default_factory=fresh
        )

    record = Record()
    assert created == ["created"]
    assert FieldCodec.encode(record) == {"id": "constructed-id"}
    plan = FieldCodec.field_plan(Record, "id")
    for raw in ({}, {"id": None}):
        for decoded in (
            FieldCodec.decode(Record, raw),
            FieldCodec.decode_bound(Record, raw),
            FieldCodec.decode_selected(Record, raw, {}, fields=("id",)),
        ):
            assert decoded.id is None
            assert FieldCodec.encode(decoded) == {}
            assert FieldCodec.project(decoded, "status") == {}
        assert plan.read(raw) is None
    assert created == ["created"]
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Record, {"id": 3})
    assert created == ["created"]


@dataclass(frozen=True)
class NoAbsentLabel(NullMember):
    """No label was recorded."""


def test_wire_absent_null_member_is_omitted_and_none_bytes_are_unchanged():
    @dataclass(frozen=True)
    class Labelled:
        label: Annotated[str | NoAbsentLabel, WireAbsentNone()] = field(
            default_factory=NoAbsentLabel
        )

    @dataclass(frozen=True)
    class NoneLabelled:
        label: Annotated[str | None, WireAbsentNone()] = None

    assert FieldCodec.encode(Labelled()) == {}
    assert FieldCodec.encode(Labelled("named")) == {"label": "named"}
    for raw in ({}, {"label": None}):
        assert FieldCodec.decode(Labelled, raw) == Labelled()
    assert FieldCodec.decode(Labelled, {"label": "named"}) == Labelled("named")
    assert json.dumps(FieldCodec.encode(NoneLabelled())) == json.dumps(FieldCodec.encode(Labelled())) == "{}"
    assert FieldCodec.encode(NoneLabelled("named")) == {"label": "named"}


def test_json_shape_schema_uses_the_same_member_annotations_as_decode():
    class SchemaShape(DeclaredFamily, JsonShapeFamily, affix="SchemaShape"):
        pass

    assert FieldCodec.value_schema(SchemaShape) == {"not": {}}
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(SchemaShape, None)

    @dataclass(frozen=True)
    class NullSchemaShape(SchemaShape, JsonShapeMember):
        value: None = None

    @dataclass(frozen=True)
    class TextSchemaShape(SchemaShape, JsonShapeMember):
        value: str

    @dataclass(frozen=True)
    class ArraySchemaShape(SchemaShape, JsonShapeMember):
        value: tuple[int, ...]

    assert FieldCodec.value_schema(SchemaShape) == {
        "anyOf": [
            {"type": "null"},
            {"type": "string"},
            {"type": "array", "items": {"type": "integer"}},
        ]
    }
    for value in (NullSchemaShape(), TextSchemaShape("text"), ArraySchemaShape((1, 2))):
        assert FieldCodec.decode(SchemaShape, FieldCodec.encode(value)) == value


def test_json_value_alias_uses_the_opaque_json_owner_everywhere():
    from pydantic import JsonValue

    payload = {"postimage": [None, True, 0, 0.0, "text", {"nested": [9007199254740993]}]}
    for annotation in (JsonValue, Annotated[JsonValue, "documented postimage"]):
        assert FieldCodec.encode(payload, annotation) == payload
        assert FieldCodec.decode(annotation, payload) == payload
        assert FieldCodec.project(payload, "report", annotation) == payload
        assert FieldCodec.value_schema(annotation) == {}
        for invalid in ({1: "non-text key"}, {"nested": float("nan")}, {"nested": object()}):
            for operation in (
                lambda: FieldCodec.encode(invalid, annotation),
                lambda: FieldCodec.project(invalid, "report", annotation),
            ):
                with pytest.raises(TypeError):
                    operation()
            with pytest.raises(MalformedRecord):
                FieldCodec.decode(annotation, invalid)
    assert FieldCodec.decode(dict[str, JsonValue], payload) == payload
    assert FieldCodec.decode(dict[str, object], payload) == payload
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(dict[str, object], {"nested": object()})
    assert FieldCodec.value_schema(dict[str, JsonValue]) == {
        "type": "object",
        "additionalProperties": {},
    }


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


def test_subclass_union_slots_replacement_decodes_once_and_keeps_unions_independent():
    calls = []

    class First(SubclassUnion, affix=""):
        pass

    class Second(SubclassUnion, affix=""):
        pass

    def declare_number(union):
        @dataclass(frozen=True, slots=True)
        class Number(union):
            value: int

            def to_wire(self):
                return self.value

            @classmethod
            def from_wire(cls, data):
                calls.append(cls)
                return cls(FieldCodec.decode(int, data))

            @classmethod
            def schema(cls):
                return FieldCodec.value_schema(int)

        return Number

    first = declare_number(First)
    second = declare_number(Second)

    @dataclass(frozen=True, slots=True)
    class Text(OwnedWireText, First):
        pass

    @dataclass(frozen=True, slots=True)
    class Empty(NullMember, First):
        pass

    assert type(FieldCodec.decode(First, 7)) is first
    assert type(FieldCodec.decode(Second, 8)) is second
    assert calls == [first, second]
    assert First.members_with(First) == (first, Text, Empty)
    assert Second.members_with(Second) == (second,)
    assert First.member_named("number") is first
    assert FieldCodec.encode_document(Empty(), Empty) is None
    assert FieldCodec.decode_document(Empty, None) == Empty()
    assert FieldCodec.decode_document(dict[str, Empty], {"empty": None}) == {"empty": Empty()}
    assert FieldCodec.encode(first(7), First) == 7
    assert FieldCodec.decode(First, "text") == Text("text")
    assert FieldCodec.decode(First, None) == Empty()
    assert FieldCodec.value_schema(First)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Second, "text")

    @dataclass(frozen=True, slots=True)
    class Later(First):
        entries: tuple[int, ...]

        def to_wire(self):
            return FieldCodec.encode_record(self)

        @classmethod
        def from_wire(cls, data):
            return FieldCodec.decode_record(cls, data)

        @classmethod
        def schema(cls):
            return FieldCodec.record_schema(cls)

    encoded = {"entries": [1, 2]}
    assert FieldCodec.encode(Later((1, 2))) == encoded
    assert type(FieldCodec.decode(First, encoded)) is Later
    assert First.members_with(First) == (first, Text, Empty, Later)


def test_declared_scalar_capabilities_and_new_case_use_the_same_boundary():
    import sqlite3
    from pathlib import Path

    from agent_comms.typed_table import Column, TextStorage, TypedTable

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
        timestamp: Annotated[RecordedTimestamp, TimestampText]
        count: Annotated[int, HexInteger]

    value = Scalars(Path("/saved/history"), RecordedTimestamp("2026-10-01T00:00:00.000Z"), 31)
    encoded = FieldCodec.encode(value)
    assert encoded == {
        "path": "/saved/history",
        "timestamp": "2026-10-01T00:00:00.000Z",
        "count": "0x1f",
    }
    assert FieldCodec.decode(Scalars, encoded) == value
    assert FieldCodec.project(value, "status") == encoded
    assert FieldCodec.value_schema(Annotated[Path | None, PathText]) == {
        "anyOf": [
            {"type": "string", "description": "agent_comms.field_codec.PathText text"},
            {"type": "null"},
        ]
    }
    assert FieldCodec.decode(Scalars, {**encoded, "path": None}).path is None
    for key in encoded:
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(Scalars, {**encoded, key: 7})
    with pytest.raises(TypeError):
        FieldCodec.encode(value.path)

    @dataclass(frozen=True)
    class ScalarRow(TypedTable, declared_name="field_representation_rows"):
        count: Annotated[int, HexInteger, Column(storage=TextStorage)]

    with sqlite3.connect(":memory:") as db:
        ScalarRow.create(db)
        ScalarRow(31).insert(db)
        assert db.execute("SELECT count FROM field_representation_rows").fetchone() == ("0x1f",)
        assert ScalarRow.read(db.execute("SELECT count FROM field_representation_rows")) == [
            ScalarRow(31)
        ]


class Choice(DeclaredFamily, affix="Choice"):
    pass


@dataclass(frozen=True, slots=True)
class CountChoice(Choice):
    value: int


@pytest.mark.parametrize(
    "annotation,data",
    [
        (type[Choice], "absent"),
        (Choice, {"kind": "absent"}),
    ],
)
def test_family_refusal_keeps_its_own_message_once(annotation, data):
    with pytest.raises(MalformedRecord) as refused:
        FieldCodec.decode(annotation, data)
    assert str(refused.value) == "Unknown Choice name: 'absent'"
    assert type(refused.value.__cause__) is UnknownFamilyMember
    assert str(refused.value.__cause__) == str(refused.value)


@dataclass(frozen=True)
class Record:
    choice: Choice
    pair: tuple[str, str]
    aliases: dict[str, list[int]]
    label: Annotated[str, WireName("externalLabel")] = "default"
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
    assert FieldCodec.record_schema(Record)["properties"]["aliases"] == {
        "type": "object",
        "additionalProperties": {"type": "array", "items": {"type": "integer"}},
    }
    assert FieldCodec.value_schema(dict[int, str]) == {
        "type": "array",
        "items": {
            "type": "array",
            "prefixItems": [{"type": "integer"}, {"type": "string"}],
            "minItems": 2,
            "maxItems": 2,
        },
    }
    assert FieldCodec.encode({1: "x"}, dict[int, str]) == [[1, "x"]]
    assert FieldCodec.decode(dict[int, str], [[1, "x"]]) == {1: "x"}
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
        second: Annotated[str, WireName("first")]

    with pytest.raises(TypeError):
        FieldCodec.encode(Bad("a", "b"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {object(): "x"}])
def test_non_json_values_rejected(value):
    with pytest.raises(TypeError):
        FieldCodec.encode(value)


def test_tuple_shape_and_strict_bool():
    with pytest.raises(ValueError):
        FieldCodec.decode(tuple[str, str], ["one"])
    with pytest.raises(ValueError):
        FieldCodec.decode(bool, 1)


def test_union_success_releases_rejected_payload_and_failure_keeps_first_cause():
    from weakref import ref

    class Payload(list):
        pass

    payload = Payload([1])
    original = ref(payload)
    assert FieldCodec.decode(tuple[str, ...] | list[int], payload) == [1]
    del payload
    assert original() is None

    with pytest.raises(ValueError) as failure:
        FieldCodec.decode(str | int, None)
    assert isinstance(failure.value.__cause__, ValueError)
    from agent_comms.field_codec import compile_decoder

    first = compile_decoder(str | int).alternatives[0]
    assert str(failure.value.__cause__) == f"Expected {first.target}, received NoneType"


def test_scalar_dispatch_keeps_representation_and_exact_type_contracts():
    import math
    from typing import Any, Literal

    from agent_comms.field_codec import AcceptsNull, FieldRepresentation

    class NullableInteger(AcceptsNull, FieldRepresentation):

        @classmethod
        def encode(cls, value):
            return value

        @classmethod
        def decode(cls, value):
            return 17 if value is None else FieldCodec.decode(int, value)

    assert FieldCodec.decode(Annotated[int, NullableInteger], None) == 17
    assert FieldCodec.decode(Annotated[int, "non-representation"], 4) == 4
    with pytest.raises(TypeError):
        FieldCodec.decode(Annotated[int, NullableInteger, PathText], 4)
    for target, value in ((str, "text"), (int, 4), (bool, False), (type(None), None), (float, 4)):
        assert FieldCodec.decode(target, value) is value

    class DerivedInt(int):
        pass

    class DerivedStr(str):
        pass

    for target, value in (
        (int, True),
        (bool, 1),
        (str, DerivedStr("text")),
        (int, DerivedInt(4)),
        (float, True),
        (float, math.inf),
        (float, math.nan),
        (int, None),
    ):
        with pytest.raises(ValueError):
            FieldCodec.decode(target, value)
    # An unsupported declaration is a programming error, not malformed data.
    with pytest.raises(TypeError):
        FieldCodec.decode(DerivedInt, 4)
    assert FieldCodec.decode(int | None, None) is None
    assert FieldCodec.decode(Literal[True, 1], True) is True
    assert type(FieldCodec.decode(Literal[True, 1], 1)) is int
    with pytest.raises(ValueError):
        FieldCodec.decode(Literal[1], True)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Any, math.inf)


def test_record_dispatch_precedes_dynamic_scalar_equality():
    class ScalarEqualMeta(type):
        def __eq__(cls, other):
            return other is str or type.__eq__(cls, other)

        __hash__ = type.__hash__

    @dataclass
    class DynamicRecord(metaclass=ScalarEqualMeta):
        text: str

    # Arbitrary annotation equality must not run before record construction.
    assert FieldCodec.decode(DynamicRecord, {"text": "original"}) == DynamicRecord("original")
    assert FieldCodec.encode(DynamicRecord("original")) == {"text": "original"}


def test_reserved_family_tag_cannot_be_shadowed_by_a_field():
    class Local(DeclaredFamily):
        pass

    @dataclass
    class Invalid(Local):
        kind: str

    with pytest.raises(TypeError):
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
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(Choice, {"kind": "count"})


@pytest.mark.parametrize("value", ["unknown", 1, True, None, {}, {"kind": "count"}])
def test_nominal_class_reference_rejects_unknown_names_and_wrong_shapes(value):
    with pytest.raises(ValueError):
        FieldCodec.decode(ChoiceReference, {"choice": value})


def test_request_schema_preserves_nullable_fields_and_owned_nonnull_constraint():
    @dataclass(frozen=True)
    class Request:
        nullable: str | None = None
        nonnull: Annotated[str | None, WireNonNull(True)] = None

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
        project: Annotated[str, WireName("externalProject")]

    @dataclass(frozen=True)
    class Request:
        scope: Scope

    def alternatives():
        return FieldCodec.record_schema(Request)["properties"]["scope"]["oneOf"]

    for value in (CurrentScope(), ExplicitScope("/project")):
        encoded = FieldCodec.encode(value)
        (schema,) = [
            item
            for item in alternatives()
            if item["properties"]["selection"]["const"] == encoded["selection"]
        ]
        assert set(schema["required"]) <= encoded.keys()
        assert encoded.keys() <= schema["properties"].keys()
        assert schema["additionalProperties"] is False
        assert FieldCodec.decode(Scope, encoded) == value
        assert FieldCodec.decode(Request, {"scope": encoded}) == Request(value)

    assert FieldCodec.value_schema(type[Scope])["enum"] == list(Scope.names())
    # Named APIs select a declaration outside their payload; nested values
    # select it inside the object. Both use the same declared field metadata.
    assert FieldCodec.record_schema(ExplicitScope)["required"] == ["externalProject"]
    assert "selection" not in FieldCodec.record_schema(ExplicitScope)["properties"]

    @dataclass(frozen=True)
    class NewScope(ExplicitScope):
        reason: str

    new = NewScope("/project", "new declaration")
    encoded = FieldCodec.encode(new)
    (schema,) = [
        item
        for item in alternatives()
        if item["properties"]["selection"]["const"] == encoded["selection"]
    ]
    assert set(schema["required"]) == {"selection", "externalProject", "reason"}
    assert FieldCodec.decode(Scope, encoded) == new
    assert len(FieldCodec.value_schema(ExplicitScope)["oneOf"]) == 2


def test_member_cannot_write_a_second_family_discriminator():
    class Scope(DeclaredFamily, affix="Scope"):
        family_discriminator = "selection"

    with pytest.raises(TypeError):

        class BrokenScope(Scope):
            family_discriminator = "another"


def test_saved_tool_start_decodes_its_own_tool_tag_and_nullable_arguments():
    from agent_comms.tool_declaration import ToolDeclaration
    from agent_comms.transcript_events import ToolStartTranscript

    arguments = {"from": "sender", "to": "receiver", "body": "hello"}
    event = transcript_records.event(
        ToolStartTranscript,
        position=0,
        tool_call_id="call-1",
        tool_name="comms_send",
        raw_input=arguments,
        tool=ToolDeclaration.record("comms_send", arguments, call_id="case"),
    )
    encoded = FieldCodec.encode(event)
    assert encoded["tool"]["tool"] == "comms_send"
    assert "kind" not in encoded["tool"]
    assert FieldCodec.decode(ToolStartTranscript, encoded) == event


def test_uninhabited_family_schema_rejects_every_value_without_a_manual_roster():
    class Empty(DeclaredFamily, affix="Empty"):
        pass

    assert FieldCodec.value_schema(Empty) == {"not": {}}
    with pytest.raises(ValueError):
        FieldCodec.decode(Empty, {"kind": "unregistered"})


@dataclass(frozen=True)
class InlinePair(InlineRecord):
    number: int
    text: str

    def to_wire(self):
        return FieldCodec.encode_record(self)

    @classmethod
    def from_wire(cls, data):
        return FieldCodec.decode_record(cls, data)


@dataclass(frozen=True)
class Pair:
    number: int
    text: str


@dataclass(frozen=True)
class InlineContainer:
    pair: InlinePair


@dataclass(frozen=True)
class FieldInlineContainer:
    pair: Annotated[Pair, WireInline()]


@pytest.mark.parametrize(
    "value", [InlineContainer(InlinePair(7, "seven")), FieldInlineContainer(Pair(7, "seven"))]
)
def test_inline_record_owns_keys_for_encode_decode_schema_and_projection(value):
    container, wire = type(value), {"number": 7, "text": "seven"}
    assert FieldCodec.encode(value) == wire
    assert FieldCodec.decode(container, wire) == value
    assert FieldCodec.project(value, "report") == wire
    assert set(FieldCodec.record_schema(container)["required"]) == wire.keys()
    with pytest.raises(
        MalformedRecord, match=f"Missing required fields for {container.__name__}: number"
    ):
        FieldCodec.decode(container, {"text": "seven"})
    with pytest.raises(ValueError):
        FieldCodec.decode(container, {**wire, "other": 1})


@dataclass(frozen=True)
class TypeInlineClash:
    pair: InlinePair
    number: int


@dataclass(frozen=True)
class FieldInlineClash:
    text: str
    pair: Annotated[Pair, WireInline()]


@dataclass(frozen=True)
class FamilyInlineClash:
    choice: Annotated[Choice, WireInline()]
    kind: str


@pytest.mark.parametrize("declaration", [TypeInlineClash, FieldInlineClash, FamilyInlineClash])
def test_inlined_key_clash_refuses_the_declaration(declaration):
    with pytest.raises(TypeError):
        FieldCodec.record_schema(declaration)


@pytest.mark.parametrize(
    "wire,expected",
    [
        ({"notice": False}, ConversationRole()),
        ({"notice": False, "membership": None}, ConversationRole()),
        ({"notice": True}, NoticeRole()),
        ({"notice": True, "membership": None}, NoticeRole()),
    ],
)
def test_missing_and_null_membership_decode_to_the_same_role(wire, expected):
    assert MessageRole.from_wire(wire) == expected


@pytest.mark.parametrize(
    "role,external",
    [
        (ConversationRole(), {}),
        (NoticeRole(), {"notice": True}),
        (MembershipConversationRole(MembershipChange.JOINED), {"membership": "joined"}),
        (MembershipNoticeRole(MembershipChange.LEFT), {"membership": "left", "notice": True}),
    ],
)
def test_message_role_keeps_the_exact_external_envelope(role, external):
    message = Message("sender", "reader", "hello", InfoKind, timestamp=1.0, role=role)
    wire = {
        "seq": 0,
        "id": message.message_id,
        "from": "sender",
        "to": "reader",
        "ts": 1.0,
        "type": "info",
        "text": "hello",
        "sender_role": "agent",
        **external,
    }
    assert message.to_wire() == wire
    assert Message.from_wire(wire) == message
    assert "role" not in FieldCodec.record_schema(Message)["properties"]


@dataclass(frozen=True)
class _SavedRow:
    path: Annotated[Path, PathText]
    at: Annotated[RecordedTimestamp, TimestampText]
    count: int


@pytest.mark.parametrize(
    "wire,refusal",
    [
        ({"path": 7, "at": "2026-10-18T12:00:00+00:00", "count": 1}, "received int"),
        ({"path": "/saved", "at": 1.5, "count": 1}, "received float"),
        (
            {"path": "/saved", "at": "2026-10-18T12:00:00+00:00"},
            "Missing required fields for _SavedRow: count",
        ),
    ],
)
def test_refused_record_data_raises_the_declared_error(wire, refusal):
    with pytest.raises(MalformedRecord) as raised:
        FieldCodec.decode(_SavedRow, wire)
    from agent_comms.field_codec import RecordProblems

    assert type(raised.value) is RecordProblems
    assert len(raised.value.problems) == 1
    assert "fix:" in str(raised.value)


def test_record_reports_missing_unknown_and_malformed_siblings_together():
    from agent_comms.field_codec import RecordProblems

    with pytest.raises(RecordProblems) as raised:
        FieldCodec.decode(_SavedRow, {"path": 7, "at": 1.5, "extra": 0})
    assert set(raised.value.problems) == {"path", "at", "count", "extra"}
    assert all("fix:" in problem for problem in raised.value.problems.values())


def test_declaration_bug_passes_through_a_data_boundary_as_type_error():
    from agent_comms.bus_source_page import SourcePage

    @dataclass(frozen=True, kw_only=True)
    class TwoRepresentations(SourcePage):
        saved: Annotated[Path, PathText, TimestampText]

    with pytest.raises(TypeError):
        TwoRepresentations.capture(saved="/saved")


def test_identity_and_digest_text_is_one_rule_at_every_reader(tmp_path):
    from agent_comms.bus_source_page import AddressedPage
    from agent_comms.errors import RelationViolationError
    from agent_comms.field_codec import Id128Text
    from agent_comms.private_registry_guard import PrivateRegistryGuard
    from agent_comms.text_digest import TextDigest

    minted = Id128Text.mint()
    # A declared field decodes through FieldCodec; the guard calls the representation itself.
    assert AddressedPage.capture(lookup=minted).lookup == minted
    assert PrivateRegistryGuard(tmp_path / "registry.json", minted).root_id.hex() == minted
    for invalid in (minted.upper(), minted[:31], int(minted, 16)):
        with pytest.raises(MalformedRecord):
            AddressedPage.capture(lookup=invalid)
        with pytest.raises(RelationViolationError):
            PrivateRegistryGuard(tmp_path / "registry.json", invalid)
    with pytest.raises(MalformedRecord):
        TextDigest("a" * 63)
