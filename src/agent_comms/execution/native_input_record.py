"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from agent_comms.coordination.assignment_states import TriagePendingAssignment
from agent_comms.coordination.contracts import validate_execution_id
from agent_comms.coordination.errors import IdentityConflict, UnrecordedNativeContext
from agent_comms.coordination.tables.assignments import ExecutionAssignmentLink, WakeAssignment
from agent_comms.coordination.tables.executions import ExecutionRecord
from agent_comms.coordination.tables.participants import OwnerGenerations, OwnerRecord
from agent_comms.coordination.tables.responses import ResponseObligation
from agent_comms.execution.native_input_proof import NativeContextProof
from agent_comms.execution.native_integer import PositiveNativeIntegerBounds
from agent_comms.execution.native_session_reopen import PiSessionId
from agent_comms.execution.transcript_routes import InputDisplay, NoDisplayText
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    AssignmentId,
    FieldCodec,
    InputId,
    JsonShapeFamily,
    JsonShapeMember,
    MalformedRecord,
    NativeContext,
    ThreadLookup,
)
from agent_comms.storage.typed_table import (
    LOCAL_TABLE_SOURCE,
    SqlStorage,
    TableSource,
    TextStorage,
    sql_literal,
)

if TYPE_CHECKING:
    from sqlite3 import Connection

    from agent_comms.coordination.assignment_states import AssignmentState
    from agent_comms.execution.cursor_owner import CursorOwner
    from agent_comms.execution.historical_native_inputs import HistoricalNativeInput
    from agent_comms.execution.native_entries import (
        MessageEntry,
        RecordedEntry,
        TranscriptProjection,
    )
    from agent_comms.execution.native_runtime_input import NativeRuntimeInput
    from agent_comms.messaging.message_reference import MessageReference
    from agent_comms.transcript.transcript_events import TranscriptEvent


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

        return entry.events(replace(
            projection, input_display=InputDisplay(user.require_input_id(), NoDisplayText())
        ))

    def require_attempt(self) -> FullNativeExecution:
        raise IdentityConflict("Native triage input has no execution attempt")

    def published_replies(self, db: sqlite3.Connection, owner_lookup: ThreadLookup) -> tuple[MessageReference, ...]:
        """TRIAGE has no execution publication; FULL owns that relation."""
        return ()

    def matches_execution(self, execution: ExecutionRecord, owner_lookup: ThreadLookup) -> bool:
        return False

    @abstractmethod
    def retried_in(self, db: sqlite3.Connection, input_id: InputId) -> bool:
        """Whether a later attempt replaced this input, so it no longer proves its source."""

    def proves_full_source(self, proof: HistoricalNativeInput) -> bool:
        return False

    @classmethod
    @abstractmethod
    def source_membership_sql(
        cls, *, table_source: TableSource = LOCAL_TABLE_SOURCE,
    ) -> str:
        """Select original membership from this query's actual table source."""

    def source_assignment_ids(
        self, db: Connection, input_id: InputId, *,
        table_source: TableSource = LOCAL_TABLE_SOURCE,
    ) -> tuple[AssignmentId, ...]:
        return tuple(row.assignment_id for row in self.source_assignments(
            db, input_id, table_source=table_source,
        ))

    def source_assignments(
        self, db: Connection, input_id: InputId, *,
        table_source: TableSource = LOCAL_TABLE_SOURCE,
    ) -> tuple[WakeAssignment, ...]:
        """Membership leads: a missing claim reaches the required-row decoder.

        An inner join would silently erase a declared source whose claim was
        absent, making corrupted membership look like a smaller valid group.
        """
        columns = ",".join(f"c.{column}" for column in WakeAssignment.columns())
        originals = WakeAssignment.read(db.execute(
            f"SELECT {columns} FROM ({self.source_membership_sql(table_source=table_source)}) m "
            f"LEFT JOIN {WakeAssignment.qualified_table(table_source)} c "
            "ON m.assignment_id=c.assignment_id "
            "WHERE m.input_id=? ORDER BY m.ordinal", (input_id,),
        ))
        return tuple(originals)

    @abstractmethod
    def record_sources(
        self, db: Connection, input_id: InputId, assignments: tuple[WakeAssignment, ...],
    ) -> None:
        """Record only membership which is not already owned by execution claims."""

    @abstractmethod
    def historical_proof(
        self, record: NativeRuntimeInput, *, lifecycle: AssignmentState, **source,
    ) -> HistoricalNativeInput:
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

    def retried_in(self, db: sqlite3.Connection, input_id: InputId) -> bool:
        """A burned attempt no longer proves its source: its claim went back to
        triage-pending (the replacement is owed) or a later attempt already holds it."""
        members = f"({self.source_membership_sql()})"
        claims = WakeAssignment.qualified_table(LOCAL_TABLE_SOURCE)
        superseded, = db.execute(
            f"SELECT EXISTS (SELECT 1 FROM {members} s WHERE s.input_id=? AND ("
            f"EXISTS (SELECT 1 FROM {members} later WHERE later.assignment_id=s.assignment_id "
            "AND later.attempt>s.attempt) OR "
            f"EXISTS (SELECT 1 FROM {claims} c WHERE c.assignment_id=s.assignment_id "
            f"AND c.disposition={sql_literal(TriagePendingAssignment)})))", (input_id,),
        ).fetchone()
        return bool(superseded)

    @classmethod
    def source_membership_sql(
        cls, *, table_source: TableSource = LOCAL_TABLE_SOURCE,
    ) -> str:
        from agent_comms.execution.triage_native_sources import TriageNativeSource

        return (f"SELECT s.input_id, s.assignment_id, s.ordinal, s.attempt "
                f"FROM {TriageNativeSource.qualified_table(table_source)} s")

    def record_sources(
        self, db: Connection, input_id: InputId, assignments: tuple[WakeAssignment, ...],
        *, attempt: int = 0,
    ) -> None:
        from agent_comms.execution.triage_native_sources import TriageNativeSource

        if not assignments:
            raise IdentityConflict("Native input membership names at least one original source")
        try:
            for ordinal, row in enumerate(assignments):
                TriageNativeSource(input_id, ordinal, row.assignment_id, attempt).insert(db)
        except sqlite3.IntegrityError as error:
            # Only (assignment_id, attempt) is UNIQUE here; a key, CHECK or guard refusal is a bug.
            if error.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise
            raise IdentityConflict("Native input membership must name distinct original sources") from error

    @classmethod
    def from_columns(cls, execution_id: NativeColumn, attempt_ordinal: NativeColumn) -> TriageNativeExecution:
        execution_id.require_unrecorded()
        attempt_ordinal.require_unrecorded()
        return cls()

    def binding_fields(self) -> dict[str, NativeColumn]:
        return {"execution_id": UnrecordedNativeColumn(), "attempt_ordinal": UnrecordedNativeColumn()}

    def historical_proof(
        self, record: NativeRuntimeInput, *, lifecycle: AssignmentState, **source,
    ) -> HistoricalNativeInput:
        return record.verdict.historical_proof(record, lifecycle, self, **source)


@dataclass(frozen=True)
class FullNativeExecution(NativeInputExecution):

    execution_id: str
    attempt_ordinal: int

    @classmethod
    def source_membership_sql(
        cls, *, table_source: TableSource = LOCAL_TABLE_SOURCE,
    ) -> str:
        from agent_comms.execution.native_runtime_input import NativeRuntimeInput

        # A full execution has no replacement attempt: it joins once, as attempt 0.
        return (f"SELECT n.input_id, c.assignment_id, c.ordinal, 0 AS attempt "
                f"FROM {NativeRuntimeInput.qualified_table(table_source)} n "
                f"JOIN {ExecutionAssignmentLink.qualified_table(table_source)} c "
                "ON c.execution_id=n.execution_id "
                f"WHERE n.stage={sql_literal(cls)}")

    def record_sources(
        self, db: Connection, input_id: InputId, assignments: tuple[WakeAssignment, ...],
        *, attempt: int = 0,
    ) -> None:
        if attempt:
            raise IdentityConflict("Only a triage input has a replacement attempt")
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

    def matches_execution(self, execution: ExecutionRecord, owner_lookup: ThreadLookup) -> bool:
        return (
            execution.execution_id == self.execution_id
            and execution.owner_lookup == owner_lookup
            and execution.lifecycle.current_attempt_ordinal == self.attempt_ordinal
        )

    def _execution(self, db: sqlite3.Connection) -> ExecutionRecord:
        return ExecutionRecord.require(db, execution_id=self.execution_id)

    def retried_in(self, db: sqlite3.Connection, input_id: InputId) -> bool:
        """A transient provider failure leaves this input behind a later attempt."""
        return self._execution(db).lifecycle.current_attempt_ordinal != self.attempt_ordinal

    def published_replies(self, db: sqlite3.Connection, owner_lookup: ThreadLookup) -> tuple[MessageReference, ...]:
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
    ) -> HistoricalNativeInput:
        from agent_comms.execution.historical_native_inputs import FullHistoricalNativeInput

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

    def require_recorded(self) -> NativeInputReference:
        raise UnrecordedNativeContext("Native input has no recorded context")

    def require_recorded_input(self, owner: CursorOwner, db: sqlite3.Connection) -> None:
        """An unrecorded context grants no committed-input identity."""

    def acknowledge_context(
        self, row: NativeRuntimeInput, db: sqlite3.Connection, context: NativeContextProof
    ) -> None:
        """The first verified entry is immutable; later settlement does not own it."""
        row.write_context(db, context)


@dataclass(frozen=True)
class UnrecordedNativeInputReference(NativeContextReference):
    pass


@dataclass(frozen=True)
class NativeInputReference(NativeContextReference, PositiveNativeIntegerBounds):
    """A cursor's bounded reference to original recorded native context."""

    input_id: InputId
    stage: type[NativeInputExecution]
    session_id: PiSessionId
    request_generation: int

    def __post_init__(self) -> None:
        self.require_integer(self.request_generation, "Native reference generation")
        if not self.session_id:
            raise ValueError("Native reference lacks recorded identity")

    def require_recorded(self) -> NativeInputReference:
        return self

    @classmethod
    def acquire(cls, row: NativeInputContext) -> NativeInputReference:
        """Decode the complete original SQL reference, never a partial projection."""
        try:
            result = FieldCodec.construct(
                cls,
                dict(
                    input_id=row.context_value("input_id"),
                    stage=row.reference_stage,
                    session_id=row.reference_session_id,
                    request_generation=row.context_value("request_generation"),
                ),
            )
            return result
        except MalformedRecord as error:
            raise IdentityConflict("Native recorded context reference is incomplete") from error

    def acknowledge_context(
        self, row: NativeRuntimeInput, db: sqlite3.Connection, context: NativeContextProof
    ) -> None:
        row.require_context_proof().require_continuation(context)

    def require_recorded_input(self, owner: CursorOwner, db: sqlite3.Connection) -> None:
        owner.require_recorded_reference(db, self)


class NativeInputRecord(OwnerRecord, ABC):
    input_id: InputId
    execution: NativeInputExecution
    owner_lookup: ThreadLookup
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
    request_generation: NativeColumn

    @property
    @abstractmethod
    def context_anchor(self) -> NativeColumn:
        """Original storage member that distinguishes unrecorded context."""

    @property
    def reference_stage(self) -> type[NativeInputExecution]:
        return self.context_value("stage")

    @property
    def reference_session_id(self) -> object:
        """Acquire the session through its existing NativeContext column directive."""
        return self.context_value("session_id")

    def context_columns(self, context: NativeContextProof) -> dict[str, object]:
        """Build the context group through each column's declared codec, not a caller roster."""
        return {
            plan.name: plan.value.decode(FieldCodec.encode(getattr(context, plan.name)))
            for plan in FieldCodec.field_plans(type(self))
            if NativeContext.single(plan.annotation)
        }

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
        reference = NativeInputReference.acquire(self)
        reference_fields = {name for name, _annotation in FieldCodec.record_fields(type(reference))}
        try:
            for plan in FieldCodec.field_plans(type(self)):
                if plan.name not in reference_fields:
                    for directive in NativeContext.single(plan.annotation):
                        FieldCodec.decode(directive.value, getattr(self, plan.name).value)
        except MalformedRecord as error:
            raise IdentityConflict("Native recorded context group is partial") from error
        return reference
