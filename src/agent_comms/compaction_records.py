"""Canonical journal table declarations and record invariants; no admission minting."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from .child_process import ProcessIdentity
from .compaction_errors import CompactionJournalError
from .compaction_identity import (
    FreshCoverageIdentity,
    SelectedCommitReference,
    SummaryOperationIdentity,
)
from .compaction_states import (
    CommittedNativeOutcome,
    OperationState,
    PendingPublication,
    PublicationState,
    SummaryState,
    sql_names,
)
from .field_codec import FieldCodec
from .input_disposition import InputDocument
from .owner_compaction_settings import PiCompactionSettings
from .pi_summary_payloads import SelectedModel
from .selected_source import SelectedSource
from .retained_task_facts import RetainedTaskFacts
from .thread_identity import ThreadIncarnation
from .typed_table import Column, Index, TypedRow, TypedTable

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession
    from .threads import Thread


@dataclass(frozen=True)
class SelectedSummarySource:
    """Journaled request evidence, not a returned input-admission capability.

    The admission owner validates its original/turn witness; the journal retains
    its exact source data and owns only durable exclusion and native linkage.
    """

    source: SelectedSource
    selected: SelectedModel
    settings: PiCompactionSettings
    retained: RetainedTaskFacts

    def __post_init__(self):
        self.source.require_retained(self.retained)

    def summary_instructions(self, owner: Thread, instructions: str | None) -> str:
        from .turn_context import CoordinationSegment

        if owner.incarnation != self.source.incarnation:
            raise CompactionJournalError("Summary instructions belong to another current owner")
        return CoordinationSegment.capture(owner, ()).summary_instructions(instructions)

    def reservation_check(self, revision, inputs):
        return self.source.reservation_check(revision, inputs, self.retained)

    def interrupted_check(self, revision, inputs, incarnation, turn):
        return self.source.interrupted_check(revision, inputs, incarnation, turn, self.retained)

    def original_has_started(self, inputs: InputDocument) -> bool:
        return self.source.original_has_started(inputs, self.retained)

    def journal_json(self) -> str:
        record = FieldCodec.encode(self)
        return RetainedTaskFacts.frame_journal(
            record, retained_payload=RetainedTaskFacts.canonical_journal_bytes(
                FieldCodec.encode(self.retained)
            )
        )

class JournalTable:
    """Tables whose schema and transactions belong to the compaction journal."""

    def inspection(self) -> dict[str, object]:
        """The existing row declaration owns this read-only representation."""
        return FieldCodec.encode(self)


class SessionJournalHistory(JournalTable):
    """Declared history families that exclude enrolling an allegedly fresh file."""

    @classmethod
    def for_session(cls, db: sqlite3.Connection, canonical: str):
        return cls.select(db, where="session_file=?", parameters=(canonical,))

    @classmethod
    def require_pristine(cls, db: sqlite3.Connection, canonical: str) -> None:
        if any(
            table.select(db, where="session_file=? LIMIT 1", parameters=(canonical,))
            for table in TypedTable.members_with(cls)
        ):
            raise CompactionJournalError("Fresh-session history already exists")


class UnresolvedJournalHistory(SessionJournalHistory):
    """The record's declared state family owns unresolved membership."""

    @classmethod
    def unresolved_in(cls, db: sqlite3.Connection, canonical: str):
        state_family = next(field.annotation for field in cls._fields() if field.name == "state")
        return cls.select(
            db,
            where=f"session_file=? AND json_extract(state, '$.kind') IN {sql_names(state_family, unresolved=True)}",
            parameters=(canonical,),
        )


@dataclass(frozen=True)
class CompactionOperation(UnresolvedJournalHistory, TypedTable, declared_name="operations"):
    commit_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str
    intent_json: str
    state: OperationState
    evidence_json: str | None

    def inspection(self) -> dict[str, object]:
        """Expose the original intent view beside its unchanged proof bytes.

        A journal projection is deliberately not a CompactionSource decoder.
        State and evidence remain those of this original operation, even UNKNOWN.
        """
        return dict(super().inspection(), intent=json.loads(self.intent_json))

    indexes = (
        Index(
            ("session_file",),
            unique=True,
            where=f"json_extract(state, '$.kind') IN {sql_names(OperationState, unresolved=True)}",
        ),
    )

    def require_summary_link(
        self, attempt: SelectedSummaryAttempt, *, admit_original: bool
    ) -> None:
        self.state.require_committed(self.commit_id)
        try:
            reference = SelectedCommitReference.from_intent(json.loads(self.intent_json))
        except (ValueError, TypeError) as error:
            raise CompactionJournalError(
                "Exact committed native result required to link"
            ) from error
        if reference.identity(self.session_file) != attempt.identity:
            raise CompactionJournalError("Exact committed native result required to link")
        if admit_original:
            reference.require_source(attempt.source_json)

    def committed_outcome(self) -> CommittedNativeOutcome:
        self.state.require_committed(self.commit_id)
        from .compaction_states import NativeOutcome

        return NativeOutcome.read(self.evidence_json).require_committed()

    def represents_summary(self, attempt: SelectedSummaryAttempt) -> bool:
        """Read-only original linkage, never an input-admission capability."""
        return self.state.represents_summary(self, attempt)

    def publication(self) -> CompactionPublication:
        try:
            committed = self.committed_outcome()
        except (ValueError, TypeError) as error:
            raise CompactionJournalError("Exact committed native metadata required") from error
        return CompactionPublication(
            self.commit_id,
            self.session_file,
            committed.publication_json(self.commit_id),
            PendingPublication(),
        )


@dataclass(frozen=True)
class SelectedSummaryAttempt(
    UnresolvedJournalHistory, TypedTable, declared_name="selected_summary_attempts"
):
    """Provider reservation and its two distinct source evidence roles.

    request is the current typed semantic source. source_json is the immutable
    original byte string committed by SelectedCommitReference; it is never a
    runtime decoder input. A stopped external declaration carry authenticates
    historical requests without changing their original native proof bytes.
    Neither role issues input admission or resolves an uncertain attempt.
    """

    operation_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str
    source_json: str
    request: SelectedSummarySource
    state: SummaryState

    indexes = (
        Index(
            ("session_file",),
            unique=True,
            where=f"json_extract(state, '$.kind') IN {sql_names(SummaryState, unresolved=True)}",
        ),
    )

    @property
    def identity(self) -> SummaryOperationIdentity:
        return SummaryOperationIdentity(self.session_file, self.operation_id)

    def require_session(self, session_file: str) -> None:
        if self.session_file != session_file:
            raise CompactionJournalError("Selected summary reservation changed before commit")

    def require_transition(self, target: SummaryState) -> None:
        if not self.state.may_become(target):
            raise CompactionJournalError("Selected summary transition forbidden")

    @classmethod
    def blocking_in(
        cls,
        db: sqlite3.Connection,
        canonical: str,
        inputs: InputDocument,
    ) -> tuple[SelectedSummaryAttempt, ...]:
        return tuple(
            attempt
            for attempt in cls.for_session(db, canonical)
            if attempt.state.blocks_input(attempt, inputs)
        )

    def transition(self, db: sqlite3.Connection, target: SummaryState) -> SelectedSummaryAttempt:
        """CAS the exact captured record; lifecycle and returned row share one owner.

        Called inside an existing write transaction. This does not mint a receipt:
        terminal input authority is issued only after the caller's fsync returns.
        """
        if type(self).one(db, operation_id=self.operation_id) != self:
            raise CompactionJournalError("Selected summary changed before transition")
        if target == self.state:
            # The lifecycle may attest an identical observation (e.g. same refusal).
            return self
        self.require_transition(target)
        before = db.total_changes
        type(self).update(
            db,
            where="operation_id=? AND json_extract(state, '$.kind')=?",
            parameters=(self.operation_id, self.state.declared_name),
            state=target,
        )
        expected = replace(self, state=target)
        if (
            db.total_changes != before + 1
            or type(self).one(db, operation_id=self.operation_id) != expected
        ):
            raise CompactionJournalError("Exact selected summary transition required")
        return expected

    def original_has_started(self, inputs: InputDocument) -> bool:
        """Completed input evidence retires this barrier, never recreates a send token.

        The existing input ledger owns native-start proof. A linked/declined
        summary alone, a bound UNKNOWN input, or an unrelated started input
        cannot retire the reservation. Historical rows and IDs stay intact.
        """
        try:
            return self.request.original_has_started(inputs)
        except (KeyError, TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CompactionPublication(JournalTable, TypedTable, declared_name="publications"):
    """Local metadata-only projection, keyed by native commit ID; no recipient."""

    commit_id: str = field(
        metadata={"sql": Column(primary_key=True, references=(CompactionOperation, "commit_id"))}
    )
    session_file: str
    metadata_json: str
    state: PublicationState

    def require_operation(self, operation: CompactionOperation) -> None:
        if len(self.metadata_json.encode()) > 4096:
            raise CompactionJournalError("Compaction publication metadata changed")
        if self != operation.publication():
            raise CompactionJournalError("Compaction publication metadata changed")

    def require_metadata(self, metadata_json: str) -> None:
        if self.metadata_json != metadata_json:
            raise CompactionJournalError("Unknown or changed publication metadata")


@dataclass(frozen=True)
class PrivateRawInput(SessionJournalHistory, TypedTable, declared_name="private_raw_inputs"):
    input_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str
    status: Literal["unknown"] = "unknown"


@dataclass(frozen=True)
class EnrolledPrivateSession(JournalTable, TypedTable, declared_name="enrolled_private_sessions"):
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    session_id: str = field(metadata={"sql": Column(unique=True)})
    device: int
    inode: int
    header_sha256: str
    incarnation: ThreadIncarnation
    owner_lookup: str
    owner_generation: int
    admission_generation: int
    creator: ProcessIdentity

    @property
    def coverage_identity(self) -> FreshCoverageIdentity:
        return FreshCoverageIdentity(self.incarnation, self.creator, self.owner_lookup)

    def require_coverage(
        self,
        fresh: FreshPrivateSession,
        witness: SelectedSource,
        admission_generation: int | None,
    ) -> None:
        self.coverage_identity.require_owner(witness)
        if admission_generation is not None and admission_generation != self.admission_generation:
            raise CompactionJournalError("Fresh private owner coverage differs: admission")
        fresh.verify_saved_identity()


@dataclass(frozen=True)
class JournalSchemaObject(TypedRow):
    name: str
    sql: str


@dataclass(frozen=True)
class JournalMode(TypedRow):
    journal_mode: str


@dataclass(frozen=True)
class SyncMode(TypedRow):
    synchronous: int
