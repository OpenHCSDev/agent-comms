"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from abc import ABC
from dataclasses import dataclass, field, fields
import re
from typing import Annotated, Literal

from .field_codec import FieldCodec, TextRepresentation
from .declared_family import DeclaredFamily
from .coordination_errors import IdentityConflict

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


class NativeContextReference(DeclaredFamily, affix="Reference"):
    """Recorded context or an original row with no recorded context."""

    def require_recorded_input(self, owner, db) -> None:
        """An unrecorded context grants no committed-input identity."""


@dataclass(frozen=True)
class UnrecordedNativeInputReference(NativeContextReference):
    pass


@dataclass(frozen=True)
class NativeInputReference(NativeContextReference):
    """A cursor's bounded reference to original recorded native context."""

    input_id: Annotated[str, NativeInputIdText]
    assignment_id: str
    stage: Literal["triage", "full"]
    session_id: str
    request_generation: int

    @classmethod
    def acquire(cls, row) -> NativeInputReference:
        """Decode the complete original SQL reference, never a partial projection."""
        try:
            result = FieldCodec.decode(cls, {
                cls.family_discriminator: cls.declared_name,
                **{item.name: getattr(row, item.name) for item in fields(cls)},
            })
            if not 0 < result.request_generation <= 2**53 - 1:
                raise ValueError("Native reference generation is outside its native range")
            if not result.assignment_id or not result.session_id:
                raise ValueError("Native reference lacks recorded identity")
            return result
        except (TypeError, ValueError) as error:
            raise IdentityConflict("Native recorded context reference is incomplete") from error

    def require_recorded_input(self, owner, db) -> None:
        owner.require_recorded_reference(db, self)


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

    def require_recorded_owner(self, thread):
        """Join original coordinator naming/birth; this grants no live ownership."""
        from .bus_publication import stable_thread_lookup
        from .coordination_errors import IdentityConflict

        expected = OwnerGenerations(owner_lookup=stable_thread_lookup(thread.created_at), owner_thread=thread.name, generation=self.owner_generation)
        if self.owner_identity != expected:
            raise IdentityConflict("Native input belongs to another recorded owner")

    @property
    def identity(self) -> NativeInputIdentity:
        return NativeInputIdentity(
            self.input_id, self.assignment_id, self.stage, self.owner_identity,
            self.execution_id, self.attempt_ordinal,
        )


class NativeInputContext:
    """Decode the declared nullable SQL context group into its original state."""

    input_id: str | None
    assignment_id: str | None
    stage: str | None
    session_id: str | None
    request_generation: int | None

    @property
    def reference(self) -> NativeContextReference:
        # SQL NULL is classified at this original storage boundary only. An
        # absent session must have the entire declared context group absent;
        # a partial group is corruption, never an unrecorded reference.
        if self.session_id is None:
            try:
                for item in fields(self):
                    if item.metadata.get("native_context"):
                        FieldCodec.decode(type(None), getattr(self, item.name))
            except (TypeError, ValueError) as error:
                raise IdentityConflict("Native unrecorded context group is partial") from error
            return UnrecordedNativeInputReference()
        try:
            for item in fields(self):
                annotation = item.metadata.get("native_context")
                if annotation is not None:
                    FieldCodec.decode(annotation, getattr(self, item.name))
        except (TypeError, ValueError) as error:
            raise IdentityConflict("Native recorded context group is partial") from error
        return NativeInputReference.acquire(self)
