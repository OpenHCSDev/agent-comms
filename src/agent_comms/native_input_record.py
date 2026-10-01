"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
import re
from typing import Annotated, ClassVar

from .field_codec import FieldCodec, TextRepresentation
from .declared_family import DeclaredFamily
from .coordination_errors import IdentityConflict
from .coordination_contracts import validate_execution_id

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


class NativeInputExecution(DeclaredFamily, affix="NativeExecution"):
    """Original triage or attempt identity, never a send/replay capability."""

    def require_attempt(self) -> FullNativeExecution:
        raise IdentityConflict("Native triage input has no execution attempt")

    def matches_execution(self, execution, owner_lookup: str) -> bool:
        return False

    def proves_full_source(self, proof) -> bool:
        return False

    def proves_triage_source(self, proof, evidence) -> bool:
        return False

    @classmethod
    @abstractmethod
    def from_columns(cls, execution_id, attempt_ordinal) -> NativeInputExecution:
        """Acquire the original durable binding's external SQL projection."""
        raise NotImplementedError

    @abstractmethod
    def binding_fields(self):
        """Publish this member through the unchanged durable SQL contract."""
        raise NotImplementedError


@dataclass(frozen=True)
class TriageNativeExecution(NativeInputExecution):
    proof_order: ClassVar[int] = 0

    @classmethod
    def from_columns(cls, execution_id, attempt_ordinal) -> TriageNativeExecution:
        FieldCodec.decode(type(None), execution_id)
        FieldCodec.decode(type(None), attempt_ordinal)
        return cls()

    def binding_fields(self):
        return {"execution_id": None, "attempt_ordinal": None}

    def proves_triage_source(self, proof, evidence) -> bool:
        if not proof.expected_prompt_equality_established:
            return False
        return proof.require_triage_decision().proves_source(evidence)


@dataclass(frozen=True)
class FullNativeExecution(NativeInputExecution):
    proof_order: ClassVar[int] = 1

    execution_id: str
    attempt_ordinal: int

    def __post_init__(self):
        validate_execution_id(self.execution_id)
        if type(self.attempt_ordinal) is not int or self.attempt_ordinal < 1:
            raise IdentityConflict("Native full input requires an original attempt")

    @classmethod
    def from_columns(cls, execution_id, attempt_ordinal) -> FullNativeExecution:
        return FieldCodec.decode(cls, {
            cls.family_discriminator: cls.declared_name,
            "execution_id": execution_id,
            "attempt_ordinal": attempt_ordinal,
        })

    def binding_fields(self):
        return FieldCodec.project(self, "binding")

    def require_attempt(self) -> FullNativeExecution:
        return self

    def matches_execution(self, execution, owner_lookup: str) -> bool:
        return (
            execution.execution_id == self.execution_id
            and execution.owner_lookup == owner_lookup
            and execution.lifecycle.current_attempt_ordinal == self.attempt_ordinal
        )

    def proves_full_source(self, proof) -> bool:
        return proof.expected_prompt_equality_established


@dataclass(frozen=True, slots=True)
class NativeInputIdentity:
    """Original reservation identity, independent of registry allocation domains."""

    input_id: str
    assignment_id: str
    execution: NativeInputExecution
    owner: OwnerGenerations


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
    stage: type[NativeInputExecution]
    session_id: str
    request_generation: int

    @classmethod
    def acquire(cls, row) -> NativeInputReference:
        """Decode the complete original SQL reference, never a partial projection."""
        try:
            result = cls(
                input_id=row.input_id,
                assignment_id=row.assignment_id,
                stage=row.reference_stage,
                session_id=row.session_id,
                request_generation=row.request_generation,
            )
            result = FieldCodec.decode(cls, FieldCodec.encode(result, cls))
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
    execution: NativeInputExecution
    owner_lookup: str
    owner_thread: str
    owner_generation: int

    @property
    def reference_stage(self) -> type[NativeInputExecution]:
        return type(self.execution)

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
            self.input_id, self.assignment_id, self.execution, self.owner_identity,
        )


class NativeInputContext:
    """Decode the declared nullable SQL context group into its original state."""

    input_id: str | None
    assignment_id: str | None
    stage: type[NativeInputExecution] | None
    session_id: str | None
    request_generation: int | None

    @property
    def reference_stage(self) -> type[NativeInputExecution] | None:
        return self.stage

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
                    FieldCodec.decode(annotation, FieldCodec.encode(getattr(self, item.name), annotation))
        except (TypeError, ValueError) as error:
            raise IdentityConflict("Native recorded context group is partial") from error
        return NativeInputReference.acquire(self)
