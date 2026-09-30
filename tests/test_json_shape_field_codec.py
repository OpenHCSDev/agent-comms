"""External JSON shape selection belongs to the existing codec boundary."""

from abc import abstractmethod
from dataclasses import dataclass

import pytest

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember


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
    assert FieldCodec.encode(value) == raw
    assert FieldCodec.project(value, "status") == raw
    assert ExternalValue.decode("text") is TextValue
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


def test_ambiguous_shape_declarations_fail_and_semantic_members_stay_out():
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

    @dataclass(frozen=True)
    class OtherTextMeaning(JsonShapeMember, Meaning):
        value: str

    with pytest.raises(ValueError, match="Expected one"):
        FieldCodec.decode(Meaning, "ambiguous")
