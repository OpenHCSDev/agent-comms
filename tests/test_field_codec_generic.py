"""Generic declarations keep their bound fields through codec and schema construction."""
from dataclasses import dataclass
from typing import Annotated

import pytest

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec, MalformedRecord, NullMember, RecordMember, ReportExclude, SubclassUnion,
    TaggedRecordMember, compile_decoder,
)


@dataclass(frozen=True)
class GenericCodecRecord[Value]:
    value: Value


@dataclass(frozen=True)
class GenericCodecPair[Left, Right](GenericCodecRecord[tuple[Left, Right]]):
    pass


@dataclass(frozen=True)
class GenericCodecHidden[Value](GenericCodecRecord[Value]):
    secret: Annotated[Value, ReportExclude()]


class GenericCodecValue[Value](SubclassUnion, affix="GenericCodecValue"):
    pass


@dataclass(frozen=True)
class AbsentGenericCodecValue[Value](NullMember, GenericCodecValue[Value]):
    pass


@dataclass(frozen=True)
class CapturedGenericCodecValue[Value](RecordMember, GenericCodecValue[Value]):
    value: Value


@dataclass(frozen=True)
class FixedIntegerGenericCodecValue(RecordMember, GenericCodecValue[int]):
    integer: int


class GenericCodecTagged[Value](DeclaredFamily, affix="GenericCodecTagged"):
    pass


@dataclass(frozen=True)
class CapturedGenericCodecTagged[Value](TaggedRecordMember, GenericCodecTagged[Value]):
    value: Value


@pytest.mark.parametrize("value_type,value,wrong", [(int, 7, "7"), (str, "7", 7)])
def test_generic_record_decoding_encoding_and_schema_share_the_bound_type(value_type: type, value: object, wrong: object) -> None:
    annotation = GenericCodecRecord[value_type]
    record = GenericCodecRecord(value)
    assert FieldCodec.encode(record, annotation) == {"value": value}
    assert FieldCodec.decode(annotation, {"value": value}) == record
    assert FieldCodec.record_schema(annotation)["properties"]["value"] == FieldCodec.value_schema(value_type)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(annotation, {"value": wrong})


def test_inherited_type_expressions_bind_inside_a_record_field() -> None:
    annotation = GenericCodecPair[str, int]
    record = GenericCodecPair(("kept", 7))
    assert FieldCodec.encode(record, annotation) == {"value": ["kept", 7]}
    assert FieldCodec.decode(annotation, {"value": ["kept", 7]}) == record
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(annotation, {"value": [7, "kept"]})


@pytest.mark.parametrize("value_type,value,wrong", [(int, 7, "7"), (str, "7", 7)])
def test_generic_union_keeps_null_and_record_members_with_bound_fields(value_type: type, value: object, wrong: object) -> None:
    annotation = GenericCodecValue[value_type]
    record = CapturedGenericCodecValue(value)
    assert FieldCodec.encode(record, annotation) == {"value": value}
    assert FieldCodec.decode(annotation, {"value": value}) == record
    assert FieldCodec.encode(AbsentGenericCodecValue(), annotation) is None
    assert FieldCodec.decode(annotation, None) == AbsentGenericCodecValue()
    assert compile_decoder(annotation).comparison(record) == compile_decoder(annotation).comparison(
        FieldCodec.decode(annotation, {"value": value}))
    FieldCodec.value_schema(annotation)
    assert GenericCodecValue in FieldCodec.representation(annotation)[1].producer_owners()
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(annotation, {"value": wrong})


def test_generic_projection_retains_bound_fields_and_redaction_through_a_union() -> None:
    value = GenericCodecHidden("public", "secret")
    annotation = GenericCodecHidden[str]
    assert FieldCodec.project(value, ReportExclude, annotation) == {"value": "public"}
    wrapped = CapturedGenericCodecValue(value)
    assert FieldCodec.project(wrapped, ReportExclude, GenericCodecValue[annotation]) == {
        "value": {"value": "public"},
    }


def test_fixed_specialization_is_admitted_only_by_its_declared_generic_family() -> None:
    assert FieldCodec.decode(GenericCodecValue[int], {"integer": 3}) == FixedIntegerGenericCodecValue(3)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(GenericCodecValue[str], {"integer": 3})


def test_generic_tagged_family_retains_both_selector_and_bound_payload() -> None:
    value = CapturedGenericCodecTagged(9)
    annotation = GenericCodecTagged[int]
    encoded = FieldCodec.encode(value, annotation)
    assert FieldCodec.decode(annotation, encoded) == value
    assert encoded[GenericCodecTagged.family_discriminator] == CapturedGenericCodecTagged.declared_name
    FieldCodec.value_schema(annotation)
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(GenericCodecTagged[str], encoded)
