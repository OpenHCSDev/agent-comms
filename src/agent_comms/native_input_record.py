"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import Field, dataclass, field, fields, replace
from typing import Annotated, ClassVar, Literal

from .coordination_contracts import validate_execution_id
from .coordination_errors import IdentityConflict
from .coordination_tables.participants import OwnerGenerations
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, Id128Text, JsonShapeFamily, JsonShapeMember, MalformedRecord
from .typed_table import Column, IntegerStorage, TextStorage


@dataclass(frozen=True, slots=True)
class NativeInputCommit:
    """Original emitted input identity shared with its assembled context."""

    input_id: Annotated[str, Id128Text] = field(metadata={
        "wire_name": "inputId",
        "sql": Column(
            primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^a-f0-9]*'"
        ),
    })
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_entry_id: str = field(metadata={"wire_name": "sessionEntryId"})

    @property
    def input_commit(self) -> NativeInputCommit:
        return NativeInputCommit(self.input_id, self.session_id, self.session_entry_id)


@dataclass(frozen=True)
class NativeInputLocation(NativeInputCommit):
    """Emitted byte placement; only the original runtime input row persists it."""

    session_entry_start: int = field(kw_only=True, metadata={
        "wire_name": "sessionEntryStart", "sql": Column(check="session_entry_start>0"),
    })

    @classmethod
    def position_field(cls) -> Field[object]:
        """The placement declaration supplies its name and storage constraints."""
        position, = (item for item in fields(cls) if item.name in cls.__annotations__)
        return position

    @classmethod
    def pending_column(cls) -> Column:
        position = cls.position_field()
        return replace(position.metadata["sql"], nullable=True, storage=IntegerStorage)


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


class NativeInputExecution(DeclaredFamily, affix="NativeExecution"):
    """Original triage or attempt identity, never a send/replay capability."""

    @classmethod
    def sql_constraint(cls, column: str) -> str:
        """Preserve the original scalar SQL ABI from the execution declarations."""
        names = tuple(member.declared_name for member in cls.members_with(cls))
        return TextStorage.constraints(column, Literal[names])[0]

    def transcript_events(self, entry, projection, user):
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

    def published_replies(self, db, owner_lookup):
        """TRIAGE has no execution publication; FULL owns that relation."""
        return ()

    def matches_execution(self, execution, owner_lookup: str) -> bool:
        return False

    def proves_full_source(self, proof) -> bool:
        return False

    @classmethod
    @abstractmethod
    def source_membership_sql(cls) -> str:
        """Select the assignments included in this stage's input."""

    def source_assignment_ids(self, db, input_id) -> tuple[str, ...]:
        from .coordination_tables.assignments import WakeAssignment

        columns = ",".join(f"c.{column}" for column in WakeAssignment.columns())
        originals = WakeAssignment.read(db.execute(
            f"SELECT {columns} FROM {WakeAssignment.declared_name} c "
            f"JOIN ({self.source_membership_sql()}) m ON m.assignment_id=c.assignment_id "
            "WHERE m.input_id=? ORDER BY m.ordinal", (input_id,),
        ))
        return tuple(original.assignment_id for original in originals)

    @abstractmethod
    def record_sources(self, db, input_id, assignments) -> None:
        """Record only membership which is not already owned by execution claims."""

    @abstractmethod
    def historical_proof(self, record, *, lifecycle, **source):
        """Acquire the declared recorded-proof member from the original SQL row."""
        raise NotImplementedError

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

    def transcript_events(self, entry, projection, user):
        return [event.as_context() for event in
                super().transcript_events(entry, projection, user)]

    @classmethod
    def source_membership_sql(cls) -> str:
        from .triage_native_sources import TriageNativeSources

        return (f"SELECT s.input_id, member.value AS assignment_id, CAST(member.key AS INTEGER) AS ordinal "
                f"FROM {TriageNativeSources.declared_name} s, json_each(s.assignment_ids) member")

    def record_sources(self, db, input_id, assignments) -> None:
        from .triage_native_sources import TriageNativeSources

        TriageNativeSources(input_id, tuple(row.assignment_id for row in assignments)).insert(db)

    @classmethod
    def from_columns(cls, execution_id, attempt_ordinal) -> TriageNativeExecution:
        FieldCodec.decode(type(None), execution_id)
        FieldCodec.decode(type(None), attempt_ordinal)
        return cls()

    def binding_fields(self):
        return {"execution_id": None, "attempt_ordinal": None}

    def historical_proof(self, record, *, lifecycle, **source):
        from .selected_triage import TriageDecisionRecord

        decision = FieldCodec.decode(TriageDecisionRecord, FieldCodec.encode(record.verdict))
        return decision.historical_proof(record, lifecycle, self, **source)


@dataclass(frozen=True)
class FullNativeExecution(NativeInputExecution):
    proof_order: ClassVar[int] = 1

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

    def record_sources(self, db, input_id, assignments) -> None:
        if self.source_assignment_ids(db, input_id) != tuple(row.assignment_id for row in assignments):
            raise IdentityConflict("Full native input differs from its original execution membership")

    def __post_init__(self):
        validate_execution_id(self.execution_id)
        if FieldCodec.decode(int, self.attempt_ordinal) < 1:
            raise IdentityConflict("Native full input requires an original attempt")

    @classmethod
    def from_columns(cls, execution_id, attempt_ordinal) -> FullNativeExecution:
        return FieldCodec.decode(cls, {
            cls.family_discriminator: cls.declared_name,
            "execution_id": execution_id,
            "attempt_ordinal": attempt_ordinal,
        })

    def binding_fields(self):
        return FieldCodec.encode_fields(self)

    def require_attempt(self) -> FullNativeExecution:
        return self

    def matches_execution(self, execution, owner_lookup: str) -> bool:
        return (
            execution.execution_id == self.execution_id
            and execution.owner_lookup == owner_lookup
            and execution.lifecycle.current_attempt_ordinal == self.attempt_ordinal
        )

    def published_replies(self, db, owner_lookup):
        from .coordination_tables.executions import ExecutionRecord
        from .coordination_tables.responses import ResponseObligation
        from .message_reference import MessageReference

        execution = ExecutionRecord.one(db, execution_id=self.execution_id)
        if not self.matches_execution(execution, owner_lookup):
            return ()
        obligations = ResponseObligation.select(
            db, where="execution_id=?", parameters=(self.execution_id,)
        )
        if not obligations or not all(row.lifecycle.published for row in obligations):
            return ()
        return tuple(sorted(
            (MessageReference(row.lifecycle.receipt_seq, row.lifecycle.receipt_message_id)
             for row in obligations),
            key=lambda reference: reference.seq,
        ))

    def historical_proof(self, record, *, lifecycle, **source):
        from .historical_native_inputs import FullHistoricalNativeInput

        FieldCodec.decode(type(None), record.verdict)
        return FullHistoricalNativeInput(execution=self, **source)

    def proves_full_source(self, proof) -> bool:
        return proof.expected_prompt_equality_established



@dataclass(frozen=True, slots=True)
class NativeInputIdentity:
    """Original reservation identity, independent of registry allocation domains."""

    input_id: str
    execution: NativeInputExecution
    owner: OwnerGenerations


class NativeContextReference(DeclaredFamily, affix="Reference"):
    """Recorded context or an original row with no recorded context."""

    recorded = False

    def require_recorded(self) -> NativeInputReference:
        raise IdentityConflict("Native input has no recorded context")

    def require_recorded_input(self, owner, db) -> None:
        """An unrecorded context grants no committed-input identity."""


@dataclass(frozen=True)
class UnrecordedNativeInputReference(NativeContextReference):
    pass


@dataclass(frozen=True)
class NativeInputReference(NativeContextReference):
    """A cursor's bounded reference to original recorded native context."""

    input_id: Annotated[str, Id128Text]
    stage: type[NativeInputExecution]
    session_id: str
    request_generation: int

    recorded = True

    def require_recorded(self) -> NativeInputReference:
        return self

    @classmethod
    def acquire(cls, row) -> NativeInputReference:
        """Decode the complete original SQL reference, never a partial projection."""
        try:
            result = FieldCodec.construct(cls, dict(
                input_id=row.input_id,
                stage=row.reference_stage,
                session_id=row.session_id,
                request_generation=row.request_generation,
            ))
            result = FieldCodec.decode(cls, FieldCodec.encode(result, cls))
            if not 0 < result.request_generation <= 2**53 - 1:
                raise MalformedRecord("Native reference generation is outside its native range")
            if not result.session_id:
                raise MalformedRecord("Native reference lacks recorded identity")
            return result
        except MalformedRecord as error:
            raise IdentityConflict("Native recorded context reference is incomplete") from error

    def require_recorded_input(self, owner, db) -> None:
        owner.require_recorded_reference(db, self)


class NativeInputRecord(ABC):
    input_id: str
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
        from .coordination_errors import IdentityConflict

        expected = OwnerGenerations(owner_lookup=thread.incarnation.lookup, owner_thread=thread.name, generation=self.owner_generation)
        if self.owner_identity != expected:
            raise IdentityConflict("Native input belongs to another recorded owner")

    @property
    def identity(self) -> NativeInputIdentity:
        return NativeInputIdentity(
            self.input_id, self.execution, self.owner_identity,
        )


class NativeInputContext:
    """Decode the declared nullable SQL context group into its original state."""

    input_id: str | None
    stage: type[NativeInputExecution] | None
    session_id: str | None
    request_generation: int | None

    @property
    @abstractmethod
    def context_anchor(self) -> str | None:
        """Original storage member that distinguishes unrecorded context."""

    @property
    def reference_stage(self) -> type[NativeInputExecution] | None:
        return self.stage

    @property
    def reference(self) -> NativeContextReference:
        # SQL NULL is classified at this original storage boundary only. An
        # absent context anchor must have the entire declared context group absent;
        # a partial group is corruption, never an unrecorded reference.
        if self.context_anchor is None:
            try:
                for item in fields(self):
                    if item.metadata.get("native_context"):
                        FieldCodec.decode(type(None), FieldCodec.encode(getattr(self, item.name)))
            except MalformedRecord as error:
                raise IdentityConflict("Native unrecorded context group is partial") from error
            return UnrecordedNativeInputReference()
        try:
            for item in fields(self):
                annotation = item.metadata.get("native_context")
                if annotation is not None:
                    FieldCodec.decode(annotation, FieldCodec.encode(getattr(self, item.name), annotation))
        except MalformedRecord as error:
            raise IdentityConflict("Native recorded context group is partial") from error
        return NativeInputReference.acquire(self)
