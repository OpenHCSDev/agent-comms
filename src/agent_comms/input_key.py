"""Stored input identity; each member owns its original text spelling."""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, InputId, MalformedRecord, TextRepresentation, TurnId

if TYPE_CHECKING:
    from .acp_extension import InputStartedUpdate


class InputKey(DeclaredFamily):
    """Each member declares its prefix once; its text is the prefix, then its body."""

    prefix: ClassVar[str]
    body: ClassVar[str]
    readers: ClassVar[tuple[Callable[[str], object], ...]]

    @abstractmethod
    def text(self) -> str: ...

    def __str__(self) -> str:
        return self.prefix + self.text()

    @classmethod
    def public_prefix(cls) -> str:
        return cls.prefix

    @classmethod
    def read(cls, match: re.Match[str]) -> InputKey:
        return cls(*(read(group) for read, group in zip(cls.readers, match.groups(), strict=True)))

    def require_sequence(self, sequence: int | None) -> None:
        if sequence is not None:
            raise ValueError("Input key cannot have a bus sequence")

    def public_text(self) -> str:
        return self.public_prefix() + self.text()

    def identify(self, update: InputStartedUpdate) -> InputStartedUpdate:
        return update


@dataclass(frozen=True)
class AcpInputKey(InputKey):
    input_id: InputId
    prefix = "acp:"
    body = "(" + InputId.spelled_by.spelling.pattern + ")"
    readers = (partial(FieldCodec.decode, InputId),)

    def text(self) -> str:
        return self.input_id

    @classmethod
    def public_prefix(cls) -> str:
        return ""

    def identify(self, update: InputStartedUpdate) -> InputStartedUpdate:
        return update.identified(self.input_id)


@dataclass(frozen=True)
class BusInputKey(InputKey):
    sequence: int
    prefix = "bus:"
    body = r"([1-9][0-9]*)"
    readers: ClassVar[tuple[Callable[[str], object], ...]] = (int,)

    def text(self) -> str:
        return str(self.sequence)

    def require_sequence(self, sequence: int | None) -> None:
        if sequence != self.sequence or self.sequence <= 0:
            raise ValueError("Bus input key and sequence disagree")


@dataclass(frozen=True)
class OwnedBusInputKey(BusInputKey):
    owner_created: float
    body = BusInputKey.body + r":owner:(.+)"
    readers = (*BusInputKey.readers, float.fromhex)

    def text(self) -> str:
        return super().text() + ":owner:" + self.owner_created.hex()

@dataclass(frozen=True)
class TurnInputKey(InputKey):
    turn_id: TurnId
    prefix = "turn:"
    body = "(" + TurnId.spelled_by.spelling.pattern + ")"
    readers = (partial(FieldCodec.decode, TurnId),)

    def text(self) -> str:
        return self.turn_id

class InputKeyText(TextRepresentation[InputKey]):
    @classmethod
    def encode(cls, value: InputKey) -> str:
        return str(value)

    @classmethod
    def from_text(cls, value: str) -> InputKey:
        for member in InputKey.members_with(InputKey):
            if match := re.fullmatch(re.escape(cls.prefix(member)) + member.body, value):
                return member.read(match)
        raise MalformedRecord("Expected a declared input key")

    @classmethod
    def prefix(cls, member: type[InputKey]) -> str:
        return member.prefix

    @classmethod
    def schema(cls) -> dict[str, str]:
        return {"type": "string"}


class PublicInputKeyText(InputKeyText):
    @classmethod
    def encode(cls, value: InputKey) -> str:
        return value.public_text()

    @classmethod
    def prefix(cls, member: type[InputKey]) -> str:
        return member.public_prefix()
