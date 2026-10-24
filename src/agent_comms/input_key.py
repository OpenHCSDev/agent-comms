"""Stored input identity; each member owns its original text spelling."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, InputId, MalformedRecord, TextRepresentation, TurnId

if TYPE_CHECKING:
    from .acp_extension import InputStartedUpdate


class InputKey(DeclaredFamily):
    @abstractmethod
    def __str__(self) -> str: ...

    @classmethod
    @abstractmethod
    def parse(cls, text: str) -> InputKey | None: ...

    def require_sequence(self, sequence: int | None) -> None:
        if sequence is not None:
            raise ValueError("Input key cannot have a bus sequence")

    def public_text(self) -> str:
        return str(self)

    @classmethod
    def parse_public(cls, text: str) -> InputKey | None:
        return cls.parse(text)

    def identify(self, update: InputStartedUpdate) -> InputStartedUpdate:
        return update


@dataclass(frozen=True)
class AcpInputKey(InputKey):
    input_id: InputId

    def __str__(self) -> str:
        return "acp:" + self.input_id

    @classmethod
    def parse(cls, text: str) -> AcpInputKey | None:
        if match := re.fullmatch("acp:(" + InputId.spelled_by.spelling.pattern + ")", text):
            return cls(FieldCodec.decode(InputId, match[1]))
        return None

    @classmethod
    def parse_public(cls, text: str) -> AcpInputKey | None:
        if re.fullmatch(InputId.spelled_by.spelling.pattern, text):
            return cls(FieldCodec.decode(InputId, text))
        return None

    def public_text(self) -> str:
        return FieldCodec.encode(self.input_id)

    def identify(self, update: InputStartedUpdate) -> InputStartedUpdate:
        return update.identified(self.input_id)


@dataclass(frozen=True)
class BusInputKey(InputKey):
    sequence: int

    def __str__(self) -> str:
        return "bus:" + str(self.sequence)

    @classmethod
    def parse(cls, text: str) -> BusInputKey | None:
        if match := re.fullmatch(r"bus:([1-9][0-9]*)", text):
            return cls(int(match[1]))
        return None

    def require_sequence(self, sequence: int | None) -> None:
        if sequence != self.sequence or self.sequence <= 0:
            raise ValueError("Bus input key and sequence disagree")


@dataclass(frozen=True)
class OwnedBusInputKey(BusInputKey):
    owner_created: float

    def __str__(self) -> str:
        return super().__str__() + ":owner:" + self.owner_created.hex()

    @classmethod
    def parse(cls, text: str) -> OwnedBusInputKey | None:
        if match := re.fullmatch(r"bus:([1-9][0-9]*):owner:(.+)", text):
            return cls(int(match[1]), float.fromhex(match[2]))
        return None

@dataclass(frozen=True)
class TurnInputKey(InputKey):
    turn_id: TurnId

    def __str__(self) -> str:
        return "turn:" + self.turn_id

    @classmethod
    def parse(cls, text: str) -> TurnInputKey | None:
        if match := re.fullmatch(f"turn:({TurnId.spelled_by.spelling.pattern})", text):
            return cls(FieldCodec.decode(TurnId, match[1]))
        return None

class InputKeyText(TextRepresentation[InputKey]):
    @classmethod
    def encode(cls, value: InputKey) -> str:
        return str(value)

    @classmethod
    def from_text(cls, value: str) -> InputKey:
        for member in InputKey.members_with(InputKey):
            if (key := cls.parse_member(member, value)) is not None:
                return key
        raise MalformedRecord("Expected a declared input key")

    @classmethod
    def parse_member(cls, member: type[InputKey], value: str) -> InputKey | None:
        return member.parse(value)

    @classmethod
    def schema(cls) -> dict[str, str]:
        return {"type": "string"}


class PublicInputKeyText(InputKeyText):
    @classmethod
    def encode(cls, value: InputKey) -> str:
        return value.public_text()

    @classmethod
    def parse_member(cls, member: type[InputKey], value: str) -> InputKey | None:
        return member.parse_public(value)
