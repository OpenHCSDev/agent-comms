"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from .coordination_contracts import validate_execution_id
from .coordination_errors import IdentityConflict, UnrecordedNativeContext
from .coordination_tables.participants import OwnerGenerations, OwnerRecord
from .declared_family import DeclaredFamily
from .field_codec import (
    FieldCodec,
    InputId,
    JsonShapeFamily,
    JsonShapeMember,
    MalformedRecord,
    NativeContext,
    WireName,
)
from .typed_table import Column, IntegerStorage, SqlStorage, TextStorage

if TYPE_CHECKING:
    from sqlite3 import Connection

    from agent_comms.coordination_tables.executions import ExecutionRecord

    from .assignment_states import AssignmentState
    from .coordination_tables.assignments import WakeAssignment
    from .cursor_owner import CursorOwner
    from .historical_native_inputs import HistoricalNativeInput
    from .message_reference import MessageReference
    from .native_entries import MessageEntry, RecordedEntry, TranscriptProjection
    from .native_runtime_input import NativeRuntimeInput
    from .transcript_events import TranscriptEvent


@dataclass(frozen=True, slots=True)
class NativeInputCommit:
    """Original emitted input identity shared with its assembled context."""

    input_id: Annotated[
        InputId,
        WireName("inputId"),
        Column(primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^a-f0-9]*'"),
    ]
    session_id: Annotated[str, WireName("sessionId")]
    session_entry_id: Annotated[str, WireName("sessionEntryId")]

    @property
    def input_commit(self) -> NativeInputCommit:
        return NativeInputCommit(self.input_id, self.session_id, self.session_entry_id)


@dataclass(frozen=True)
class NativeInputLocation(NativeInputCommit):
    """Emitted byte placement; only the original runtime input row persists it."""

    # SQL permits an uncommitted row; the emitted offset remains a required int.
    position_column: ClassVar[Column] = Column(
        check="session_entry_start>0",
        nullable=True,
        storage=IntegerStorage,
    )
    session_entry_start: Annotated[
        int, WireName("sessionEntryStart"), position_column
    ] = field(kw_only=True)


class NativeInputPosition(DeclaredFamily, JsonShapeFamily, affix="NativePosition"):
    """Decode the original SQL placement once: uncommitted NULL or a byte offset."""

    @property
    def offset(self) -> int:
        raise IdentityConflict("Native input has no committed byte placement")


@dataclass(frozen=True)
class UnrecordedNativePosition(NativeInputPosition, JsonShapeMember):
    value: None = None


@dataclass(frozen=True)
class RecordedNativePosition(NativeInputPosition, JsonShapeMember):
    value: int

    @property
    def offset(self) -> int:
        return self.value


class NativeStageStorage(SqlStorage):
    """A nullable stage column keeps the execution family's generated name CHECK."""

    sql_type = "TEXT"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return False  # Declared on the column; a NativeColumn names no family itself.

    @classmethod
    def constraints(cls, column: str, annotation: object = None) -> tuple[str, ...]:
        return (NativeInputExecution.sql_constraint(column),)


class NativeColumn(DeclaredFamily, JsonShapeFamily, affix="NativeColumn"):
    """One nullable column of an original native row: SQL NULL until its group is recorded.

    The group's owner (the stage, or the context acknowledgement) decides what a
    recorded value means; the member answers only whether the column holds one.
    """

    value: str | int | None

    def require_unrecorded(self) -> None:
        raise MalformedRecord("Native column holds a value its group does not record")

    def reference_of(self, row: NativeInputContext) -> NativeContextReference:
        """A recorded context anchor requires the whole recorded group."""
        return row.recorded_reference()

    @abstractmethod
    def holds(self, value: object) -> bool:
        """This column records exactly ``value``."""


@dataclass(frozen=True)
class UnrecordedNativeColumn(NativeColumn, JsonShapeMember):
    value: None = None

    def require_unrecorded(self) -> None:
        pass

    def reference_of(self, row: NativeInputContext) -> NativeContextReference:
        return row.unrecorded_reference()

    def holds(self, value: object) -> bool:
        return False


@dataclass(frozen=True)
class TextNativeColumn(NativeColumn, JsonShapeMember):
    value: str

    def holds(self, value: object) -> bool:
        return self.value == value


@dataclass(frozen=True)
class NumberNativeColumn(NativeColumn, JsonShapeMember):
    value: int

    def holds(self, value: object) -> bool:
        return self.value == value


class NativeInputExecution(DeclaredFamily, affix="NativeExecution"):
    """Original triage or attempt identity, never a send/replay capability."""

    @classmethod
    def sql_constraint(cls, column: str) -> str:
        """Preserve the original scalar SQL ABI from the execution declarations."""
        names = tuple(member.declared_name for member in cls.members_with(cls))
        return TextStorage.constraints(column, Literal[names])[0]

    def transcript_events(
        self, entry: RecordedEntry, projection: TranscriptProjection, user: MessageEntry,
    ) -> list[TranscriptEvent]:
        """Selected prompts consume original wire sources, never human authorship.

        This display is derived inside the original read snapshot. It is not an
        annotation, native acknowledgement or another source-membership declaration.
        """
        from dataclasses import replace

        from .transcript_routes import InputDisplay

        return entry.events(replace(
            projection, input_display=InputDisplay(user.input_id, None)
        ))

    def require_attempt(self) -> FullNativeExecution:
        raise IdentityConflict("Native triage input has no execution attempt")

    def published_replies(self, db: sqlite3.Connection, owner_lookup: str) -> tuple[MessageReference, ...]:
        """TRIAGE has no execution publication; FULL owns that relation."""
        return ()

    def matches_execution(self, execution: ExecutionRecord, owner_lookup: str) -> bool:
        return False

    def retried_in(self, db: sqlite3.Connection) -> bool:
        """TRIAGE has no attempts, so a later attempt never replaces it."""
        return False

    def proves_full_source(self, proof: HistoricalNativeInput) -> bool:
        return False

    @classmethod
    @abstractmethod
    def source_membership_sql(cls) -> str:
        """Select the assignments included in this stage's input."""

    def source_assignment_ids(self, db: Connection, input_id: InputId) -> tuple[str, ...]:
        from .coordination_tables.assignments import WakeAssignment

        columns = ",".join(f"c.{column}" for column in WakeAssignment.columns())
        originals = WakeAssignment.read(db.execute(
            f"SELECT {columns} FROM {WakeAssignment.declared_name} c "
            f"JOIN ({self.source_membership_sql()}) m ON m.assignment_id=c.assignment_id "
            "WHERE m.input_id=? ORDER BY m.ordinal", (input_id,),
        ))
        return tuple(original.assignment_id for original in originals)

    @abstractmethod
    def record_sources(self, db: Connection, input_id: InputId, assignments: tuple[WakeAssignment, ...]) -> None:
        """Record only membership which is not already owned by execution claims."""

    @abstractmethod
    def historical_proof(
        self, record: NativeRuntimeInput, *, lifecycle: AssignmentState, **source,
    ) -> HistoricalNativeInput | None:
        """Acquire the declared recorded-proof member from the original SQL row."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_columns(cls, execution_id: NativeColumn, attempt_ordinal: NativeColumn) -> NativeInputExecution:
        """Acquire the original durable binding's external SQL projection."""
        raise NotImplementedError

    @abstractmethod
    def binding_fields(self) -> dict[str, NativeColumn]:
        """Publish this member through the unchanged durable SQL contract."""
        raise NotImplementedError


@dataclass(frozen=True)
class TriageNativeExecution(NativeInputExecution):

    def transcript_events(
        self, entry: RecordedEntry, projection: TranscriptProjection, user: MessageEntry,
    ) -> list[TranscriptEvent]:
        return [event.as_context() for event in
                super().transcript_events(entry, projection, user)]

    @classmethod
    def source_membership_sql(cls) -> str:
        from .triage_native_sources import TriageNativeSources

        return (f"SELECT s.input_id, member.value AS assignment_id, CAST(member.key AS INTEGER) AS ordinal "
                f"FROM {TriageNativeSources.declared_name} s, json_each(s.assignment_ids) member")

    def record_sources(self, db: Connection, input_id: InputId, assignments: tuple[WakeAssignment, ...]) -> None:
        from .triage_native_sources import TriageNativeSources

        TriageNativeSources(input_id, tuple(row.assignment_id for row in assignments)).insert(db)

    @classmethod
    def from_columns(cls, execution_id: NativeColumn, attempt_ordinal: NativeColumn) -> TriageNativeExecution:
        execution_id.require_unrecorded()
        attempt_ordinal.require_unrecorded()
        return cls()

    def binding_fields(self) -> dict[str, NativeColumn]:
        return {"execution_id": UnrecordedNativeColumn(), "attempt_ordinal": UnrecordedNativeColumn()}

    def historical_proof(
        self, record: NativeRuntimeInput, *, lifecycle: AssignmentState, **source,
    ) -> HistoricalNativeInput | None:
        return record.verdict.historical_proof(record, lifecycle, self, **source)


@dataclass(frozen=True)
class FullNativeExecution(NativeInputExecution):

    execution_id: str
    attempt_ordinal: int

    @classmethod
    def source_membership_sql(cls) -> str:
        from .coordination_tables.assignments import ExecutionAssignmentLink
        from .native_runtime_input import NativeRuntimeInput

        return (f"SELECT n.input_id, c.assignment_id, c.ordinal "
                f"FROM {NativeRuntimeInput.declared_name} n "
                f"JOIN {ExecutionAssignmentLink.declared_name} c ON c.execution_id=n.execution_id "
                f"WHERE n.stage='{cls.declared_name}'")

    def record_sources(self, db: Connection, input_id: InputId, assignments: tuple[WakeAssignment, ...]) -> None:
        if self.source_assignment_ids(db, input_id) != tuple(row.assignment_id for row in assignments):
            raise IdentityConflict("Full native input differs from its original execution membership")

    def __post_init__(self):
        validate_execution_id(self.execution_id)
        if FieldCodec.decode(int, self.attempt_ordinal) < 1:
            raise IdentityConflict("Native full input requires an original attempt")

    @classmethod
    def from_columns(cls, execution_id: NativeColumn, attempt_ordinal: NativeColumn) -> FullNativeExecution:
        return FieldCodec.decode(cls, {
            cls.family_discriminator: cls.declared_name,
            "execution_id": FieldCodec.encode(execution_id),
            "attempt_ordinal": FieldCodec.encode(attempt_ordinal),
        })

    def binding_fields(self) -> dict[str, NativeColumn]:
        return {"execution_id": TextNativeColumn(self.execution_id),
                "attempt_ordinal": NumberNativeColumn(self.attempt_ordinal)}

    def require_attempt(self) -> FullNativeExecution:
        return self

    def matches_execution(self, execution: ExecutionRecord, owner_lookup: str) -> bool:
        return (
            execution.execution_id == self.execution_id
            and execution.owner_lookup == owner_lookup
            and execution.lifecycle.current_attempt_ordinal == self.attempt_ordinal
        )

    def _execution(self, db: sqlite3.Connection) -> ExecutionRecord:
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return ExecutionRecord.require(db, execution_id=self.execution_id)

    def retried_in(self, db: sqlite3.Connection) -> bool:
        """A transient provider failure leaves this input behind a later attempt."""
        return self._execution(db).lifecycle.current_attempt_ordinal != self.attempt_ordinal

    def published_replies(self, db: sqlite3.Connection, owner_lookup: str) -> tuple[MessageReference, ...]:
        from .coordination_tables.responses import ResponseObligation
        execution = self._execution(db)
        if not self.matches_execution(execution, owner_lookup):
            return ()
        obligations = ResponseObligation.select(
            db, where="execution_id=?", parameters=(self.execution_id,)
        )
        if not obligations or not all(row.lifecycle.published for row in obligations):
            return ()
        return tuple(
            sorted(
                (row.lifecycle.published_reference() for row in obligations),
                key=lambda reference: reference.seq,
            )
        )

    def historical_proof(
        self, record: NativeRuntimeInput, *, lifecycle: AssignmentState, **source,
    ) -> HistoricalNativeInput | None:
        from .historical_native_inputs import FullHistoricalNativeInput

        record.verdict.require_undecided()
        return FullHistoricalNativeInput(execution=self, **source)

    def proves_full_source(self, proof: HistoricalNativeInput) -> bool:
        return proof.expected_prompt_equality_established


@dataclass(frozen=True, slots=True)
class NativeInputIdentity:
    """Original reservation identity, independent of registry allocation domains."""

    input_id: InputId
    execution: NativeInputExecution
    owner: OwnerGenerations


class NativeContextReference(DeclaredFamily, affix="Reference"):
    """Recorded context or an original row with no recorded context."""

    recorded = False

    def require_recorded(self) -> NativeInputReference:
        raise UnrecordedNativeContext("Native input has no recorded context")

    def require_recorded_input(self, owner: CursorOwner, db: sqlite3.Connection) -> None:
        """An unrecorded context grants no committed-input identity."""


@dataclass(frozen=True)
class UnrecordedNativeInputReference(NativeContextReference):
    pass


@dataclass(frozen=True)
class NativeInputReference(NativeContextReference):
    """A cursor's bounded reference to original recorded native context."""

    input_id: InputId
    stage: type[NativeInputExecution]
    session_id: str
    request_generation: int

    recorded = True

    def require_recorded(self) -> NativeInputReference:
        return self

    @classmethod
    def acquire(cls, row: NativeInputContext) -> NativeInputReference:
        """Decode the complete original SQL reference, never a partial projection."""
        try:
            result = FieldCodec.construct(cls, dict(
                input_id=row.context_value("input_id"),
                stage=row.reference_stage,
                session_id=row.context_value("session_id"),
                request_generation=row.context_value("request_generation"),
            ))
            result = FieldCodec.decode(cls, FieldCodec.encode(result, cls))
            if not 0 < result.request_generation <= 2**53 - 1:
                raise MalformedRecord("Native reference generation is outside its native range")
            if not result.session_id:
                raise MalformedRecord("Native reference lacks recorded identity")
            return result
        except MalformedRecord as error:
            raise IdentityConflict("Native recorded context reference is incomplete") from error

    def require_recorded_input(self, owner: CursorOwner, db: sqlite3.Connection) -> None:
        owner.require_recorded_reference(db, self)


class NativeInputRecord(OwnerRecord, ABC):
    input_id: InputId
    execution: NativeInputExecution
    owner_lookup: str
    owner_generation: int

    @property
    def reference_stage(self) -> type[NativeInputExecution]:
        return type(self.execution)

    @property
    def identity(self) -> NativeInputIdentity:
        return NativeInputIdentity(
            self.input_id, self.execution, self.owner_identity,
        )


class NativeInputContext:
    """Decode the declared nullable SQL context group into its original state."""

    input_id: InputId | NativeColumn
    stage: type[NativeInputExecution] | NativeColumn
    session_id: str | NativeColumn
    request_generation: NativeColumn

    @property
    @abstractmethod
    def context_anchor(self) -> NativeColumn:
        """Original storage member that distinguishes unrecorded context."""

    @property
    def reference_stage(self) -> type[NativeInputExecution]:
        return self.context_value("stage")

    def context_value(self, name: str) -> object:
        """A context field decoded as its NativeContext directive declares it."""
        plan = next(plan for plan in FieldCodec.field_plans(type(self)) if plan.name == name)
        for directive in NativeContext.single(plan.annotation):
            return FieldCodec.decode(directive.value, getattr(self, name).value)
        return getattr(self, name)

    @property
    def reference(self) -> NativeContextReference:
        # SQL NULL is classified at this original storage boundary only: the
        # context anchor's member decides which group shape the row must hold.
        return self.context_anchor.reference_of(self)

    def unrecorded_reference(self) -> NativeContextReference:
        """An absent anchor needs the whole group absent; a partial group is corruption."""
        try:
            for plan in FieldCodec.field_plans(type(self)):
                for _directive in NativeContext.single(plan.annotation):
                    FieldCodec.decode(type(None), getattr(self, plan.name).value)
        except MalformedRecord as error:
            raise IdentityConflict("Native unrecorded context group is partial") from error
        return UnrecordedNativeInputReference()

    def recorded_reference(self) -> NativeContextReference:
        try:
            for plan in FieldCodec.field_plans(type(self)):
                for directive in NativeContext.single(plan.annotation):
                    FieldCodec.decode(directive.value, getattr(self, plan.name).value)
        except MalformedRecord as error:
            raise IdentityConflict("Native recorded context group is partial") from error
        return NativeInputReference.acquire(self)
