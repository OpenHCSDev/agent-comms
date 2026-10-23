"""External JSON shape selection belongs to the existing codec boundary."""

from abc import abstractmethod
from dataclasses import dataclass
import json

import pytest

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import (
    FieldCodec, JsonShapeFamily, JsonShapeMember, MalformedRecord, SingleFieldValue,
)
from agent_comms.launch_runtime import ImportPath, NoImportPath, SpecifiedImportPath
from agent_comms.native_claim_epoch import NativeClaimEpoch, RecordedNativeClaim, UnrecordedNativeClaim
from agent_comms.selected_triage import (
    AbsentTriageDecisionRecord,
    DecidedTriageRecord,
    IgnoreSelectedTriage,
    TriageDecisionRecord,
)


class ExternalValue(DeclaredFamily, JsonShapeFamily, affix="Value"):
    @abstractmethod
    def render(self): ...


@dataclass(frozen=True)
class ObjectValue(JsonShapeMember, ExternalValue):
    value: dict[str, ExternalValue]

    def render(self):
        return tuple(item.render() for item in self.value.values())


@dataclass(frozen=True)
class ArrayValue(JsonShapeMember, ExternalValue):
    value: tuple[ExternalValue, ...]

    def render(self):
        return tuple(item.render() for item in self.value)


@dataclass(frozen=True)
class TextValue(JsonShapeMember, ExternalValue):
    value: str

    def render(self):
        return self.value


@dataclass(frozen=True)
class IntegerValue(JsonShapeMember, ExternalValue):
    value: int

    def render(self):
        return str(self.value)


@dataclass(frozen=True)
class BooleanValue(JsonShapeMember, ExternalValue):
    value: bool

    def render(self):
        return str(self.value)


@dataclass(frozen=True)
class NullValue(JsonShapeMember, ExternalValue):
    value: None

    def render(self):
        return ""


def test_external_shape_family_uses_one_codec_for_children_and_new_cases():
    raw = {"details": ["failure", {"code": 400}, False, None]}
    value = FieldCodec.decode(ExternalValue, raw)
    assert isinstance(value, ObjectValue)
    assert isinstance(value.value["details"], ArrayValue)
    assert isinstance(value.value["details"].value[1].value["code"], IntegerValue)
    assert isinstance(value.value["details"].value[2], BooleanValue)
    assert isinstance(value.value["details"].value[3], NullValue)
    assert value.render() == (("failure", ("400",), "False", ""),)
    assert value.to_wire() == raw
    assert json.loads(json.dumps(value.to_wire())) == raw
    assert FieldCodec.encode(value) == raw
    assert FieldCodec.project(value, "status") == raw
    assert ExternalValue.member_named("text") is TextValue
    assert FieldCodec.decode(type[ExternalValue], "text") is TextValue
    for invalid in (object(), {1: "bad key"}, float("nan")):
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(ExternalValue, invalid)

    @dataclass(frozen=True)
    class FloatValue(JsonShapeMember, ExternalValue):
        value: float

        def render(self):
            return str(self.value)

    assert FieldCodec.decode(ExternalValue, 1.5) == FloatValue(1.5)
    assert FieldCodec.encode(FloatValue(1.5)) == 1.5
    with pytest.raises(ValueError):
        FieldCodec.decode(ExternalValue, float("nan"))


def test_one_field_encoding_preserves_distinct_cli_argument_contracts():
    @dataclass(frozen=True)
    class ScalarArgument(SingleFieldValue):
        value: int

    assert ScalarArgument(5).to_wire() == 5
    assert ScalarArgument.from_wire(5) == ScalarArgument(5)
    assert ScalarArgument.argument_options() == {"type": int}
    assert ExternalValue.argument_options() == {}
    assert ImportPath.argument_options() == {}
    assert NativeClaimEpoch.argument_options() == {}


def test_semantic_members_stay_out_of_shape_selection():
    class Meaning(DeclaredFamily, JsonShapeFamily, affix="Meaning"):
        pass

    @dataclass(frozen=True)
    class DecodedMeaning(Meaning):
        value: str

    @dataclass(frozen=True)
    class TextMeaning(JsonShapeMember, Meaning):
        value: str

        @classmethod
        def from_json_value(cls, value):
            return DecodedMeaning(value)

    assert FieldCodec.decode(Meaning, "typed") == DecodedMeaning("typed")
    assert FieldCodec.encode(DecodedMeaning("typed")) == "typed"


@pytest.mark.parametrize(("family", "wire", "expected", "refused"), [
    (NativeClaimEpoch, None, UnrecordedNativeClaim(), "5"),
    (NativeClaimEpoch, 5, RecordedNativeClaim(5), "5"),
    (TriageDecisionRecord, None, AbsentTriageDecisionRecord(), 1),
    (TriageDecisionRecord, "ignore", DecidedTriageRecord(IgnoreSelectedTriage), "IGNORE"),
    (ImportPath, None, NoImportPath(), 1),
    (ImportPath, "/opt/lib", SpecifiedImportPath("/opt/lib"), 1),
])
def test_shape_family_decodes_each_member_and_refuses_an_unowned_type(family, wire, expected, refused):
    assert FieldCodec.decode(family, wire) == expected
    assert expected.to_wire() == wire
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(family, refused)


@pytest.mark.parametrize(("annotations", "message"), [
    ({"value": str}, "Two .* owners for str"),
    ({"value": int, "other": int}, "one value field"),
    ({"value": complex}, "Unsupported JSON shape"),
])
def test_bad_shape_declaration_fails_when_the_member_is_defined(annotations, message):
    class Meaning(DeclaredFamily, JsonShapeFamily, affix="BadMeaning"):
        pass

    @dataclass(frozen=True)
    class TextMeaning(JsonShapeMember, Meaning):
        value: str

    with pytest.raises(TypeError):
        type("BadMember", (JsonShapeMember, Meaning), {"__annotations__": annotations})
