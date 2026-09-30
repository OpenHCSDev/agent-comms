"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from abc import ABC
from dataclasses import dataclass, field, fields
import re

from .field_codec import FieldCodec, TextRepresentation

from .coordination_tables.participants import OwnerGenerations


class NativeInputIdText(TextRepresentation):
    """The native-input declaration owns the external 128-bit input spelling."""

    @classmethod
    def encode(cls, value):
        return cls.decode(value)

    @classmethod
    def from_text(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError("Native input requires a 128-bit lowercase hex identity")
        return value


@dataclass(frozen=True, slots=True)
class NativeInputCommit:
    """Original emitted input identity shared with its assembled context."""

    input_id: str = field(metadata={"wire_name": "inputId"})
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_entry_id: str = field(metadata={"wire_name": "sessionEntryId"})

    @classmethod
    def from_event(cls, event):
        return FieldCodec.decode(cls, {
            item.metadata["wire_name"]: getattr(event, item.name)
            for item in fields(cls)
        })


@dataclass(frozen=True, slots=True)
class NativeInputIdentity:
    """Original reservation identity, independent of registry allocation domains."""

    input_id: str
    assignment_id: str
    stage: str
    owner: OwnerGenerations
    execution_id: str | None
    attempt_ordinal: int | None


@dataclass(frozen=True, slots=True)
class NativeInputReference:
    """A cursor's bounded reference to original recorded native context."""

    input_id: str
    assignment_id: str
    stage: str
    session_id: str
    request_generation: int


class NativeInputRecord(ABC):
    input_id: str
    assignment_id: str
    stage: str
    owner_lookup: str
    owner_thread: str
    owner_generation: int
    execution_id: str | None
    attempt_ordinal: int | None

    @property
    def owner_identity(self) -> OwnerGenerations:
        return OwnerGenerations(
            owner_lookup=self.owner_lookup,
            owner_thread=self.owner_thread,
            generation=self.owner_generation,
        )

    @property
    def identity(self) -> NativeInputIdentity:
        return NativeInputIdentity(
            self.input_id, self.assignment_id, self.stage, self.owner_identity,
            self.execution_id, self.attempt_ordinal,
        )


class NativeInputContext:
    """Rows with the original optional context group share its bounded reference."""

    input_id: str | None
    assignment_id: str | None
    stage: str | None
    session_id: str | None
    request_generation: int | None

    @property
    def reference(self) -> NativeInputReference | None:
        if self.input_id is None or self.session_id is None:
            return None
        return NativeInputReference(
            self.input_id, self.assignment_id, self.stage,
            self.session_id, self.request_generation,
        )
