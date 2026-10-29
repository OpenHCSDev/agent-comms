"""The native payload boundary, independent of runtime implementations."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import (
    ClassVar,
    Self,
)

from pydantic import JsonValue

from .declared_family import (
    DeclaredFamily,
    UnknownFamilyMember,
)
from .field_codec import (
    ExternalField,
    FieldCodec,
    MalformedRecord,
    RecordProblems,
    boundary_decoder,
)


class PiPayload(ExternalField):
    wire_tag: ClassVar[str | None] = None

    @classmethod
    def decode_external_value(cls, value: object, record: Mapping[str, object]) -> object:
        if not isinstance(value, dict):
            raise MalformedRecord(f"Expected {cls.__name__} object")
        member = cls.wire_member(value)
        return FieldCodec.construct(member, member.decode_fields(value, {}))

    @classmethod
    def wire_member(cls, value: dict[str, JsonValue]) -> type[Self]:
        return cls

    @classmethod
    @boundary_decoder
    def decode_fields(
        cls, value: dict[str, JsonValue], decoded: dict[str, object],
    ) -> dict[str, object]:
        hints = FieldCodec._types(cls)
        problems: dict[str, str] = {}
        for f, key in FieldCodec.record_fields(cls):
            if f.name in decoded:
                decoded[f.name] = FieldCodec.decode_resolved(hints[f.name], decoded[f.name])
                continue
            plan = FieldCodec.field_plan(cls, f.name)
            try:
                captured = plan.decoded_external(value)
                if f.name not in captured and (plan.constructor_required or plan.required):
                    raise MalformedRecord(f"Missing required field {key}")
                decoded.update(captured)
            except MalformedRecord as refused:
                plan.locate(problems, refused)
        if problems:
            raise RecordProblems(problems)
        return decoded

    @classmethod
    def from_wire(cls, value):
        return FieldCodec.decode_external(cls, value, {})

    def to_wire(self):
        data = FieldCodec.encode_fields(self)
        if self.wire_tag is not None and DeclaredFamily.record_tag(self):
            data[self.wire_tag] = self.wire_discriminator()
        return data

    def wire_discriminator(self):
        """Refined internal members borrow their original external declaration."""
        return FieldCodec.encode(type(self))


@dataclass(frozen=True, kw_only=True)
class Opaque(PiPayload):
    """Own the original external JSON value and its normalization."""

    payload: JsonValue

    @classmethod
    def decode_fields(
        cls, value: dict[str, JsonValue], decoded: dict[str, object],
    ) -> dict[str, object]:
        return super().decode_fields(value, dict(
            decoded, payload=FieldCodec.decode(JsonValue, value),
        ))

    def to_wire(self) -> JsonValue:
        return self.payload


class UnknownPayload(Opaque):
    """The declaration an open family selects for an unknown external tag."""


class StrictFields(PiPayload):
    """A closed native record rejects every undeclared external field."""

    @classmethod
    def decode_fields(
        cls, value: dict[str, JsonValue], decoded: dict[str, object],
    ) -> dict[str, object]:
        names = {key for _, key in FieldCodec.record_fields(cls)}
        if set(value) - names - {cls.wire_tag}:
            raise MalformedRecord(f"Unexpected {cls.__name__} fields")
        return super().decode_fields(value, decoded)


class OpenPayload(PiPayload, DeclaredFamily):
    """An external tag selects its member or the family's declared unknown payload."""

    wire_tag = "type"

    @classmethod
    def wire_member(cls, value: dict[str, JsonValue]) -> type[Self]:
        try:
            member = cls.member_named(FieldCodec.decode(str, value.get(cls.wire_tag)))
            return member.wire_case(value)
        except (UnknownFamilyMember, MalformedRecord):
            member, = cls.members_with(UnknownPayload)
            return member

    @classmethod
    def wire_case(cls, value: dict[str, JsonValue]) -> type[Self]:
        return cls
