"""Declared native input and cursor rows, never reconstructed native proof.

These original coordination.sqlite3 rows retain irreplaceable reservation,
permission to send and live-context ownership. Installation never resets them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated

from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import Participants
from agent_comms.private_runtime_schema import PrivateCoordinatorTable

from .coordination_errors import IdentityConflict, PublicationActivationBlocked, StaleFence
from .coordination_tables.participants import OwnerGenerations
from .coordination_tables.responses import ResponseObligation
from .declared_family import DeclaredFamily
from .field_codec import (
    FieldCodec, InputId, MalformedRecord, NativeContext, RootId, SlotId, ThreadLookup,
)
from .native_pi import NativeContextRecord
from .native_session_reopen import NativeSessionIdentity, NoPiSessionId, NoSessionRetention, PiSessionId
from .native_tool_call import ToolCallId
from .native_claim_epoch import NativeClaimEpoch, UnrecordedNativeClaim
from .tool_denial import SelectedToolDenied
from .native_input_record import (
    NativeColumn,
    NativeInputContext,
    NativeInputExecution,
    NativeInputLocation,
    NativeInputPosition,
    NativeInputRecord,
    TriageNativeExecution,
    NativeStageStorage,
    NumberNativeColumn,
    RecordedNativePosition,
    TextNativeColumn,
    UnrecordedNativeColumn,
    UnrecordedNativePosition,
)
from .selected_triage import (
    AbsentTriageDecisionRecord,
    RecordedTriageText,
    TriageDecisionRecord,
)
from .typed_table import (
    TableReference,
    Column,
    FinalizedColumn,
    FrozenColumn,
    IntegerStorage,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
    RequiredUpdateColumns,
    StableReferenceColumns,
    TextStorage,
    TypedRow,
    WithoutRowid,
    WriteOnceColumns,
    sql_literal,
)

if TYPE_CHECKING:
    from .native_pi import NativeContextProof
    from .native_session_reopen import NativeSessionIdentity


class NativeRuntimeTable(PrivateCoordinatorTable):
    """Capability identifying the native-runtime schema's declared tables."""

    __slots__ = ()
    schema_error = PublicationActivationBlocked


@dataclass(frozen=True)
class PublishedReplyRevision(TypedRow):
    """Derived identity of this source's immutable published native relations."""

    through_seq: int
    inputs: int


@dataclass(frozen=True)
class NativeRuntimeInput(
    WriteOnceColumns,
    FinalizedColumn,
    RequiredUpdateColumns,
    NoDelete,
    NativeInputRecord,
    NativeInputContext,
    NativeRuntimeTable,
    WithoutRowid,
):
    @classmethod
    def notification_context_sql(cls) -> str:
        """Native context evidence for the caller's bounded assignment window ``w``."""
        triage_stage = sql_literal(TriageNativeExecution)
        return (
            "WITH member AS MATERIALIZED (SELECT m.input_id,m.assignment_id,m.ordinal "
            f"FROM ({cls.source_membership_sql()}) m "
            "WHERE m.assignment_id IN (SELECT assignment_id FROM w)) "
            "SELECT w.assignment_id,"
            f"EXISTS (SELECT 1 FROM {cls.declared_name} n "
            "JOIN member ON member.input_id=n.input_id "
            "WHERE member.assignment_id=w.assignment_id "
            f"AND n.stage={triage_stage} AND n.session_entry_id IS NULL) AS triage_inflight,"
            f"(SELECT n.committed_at_ms FROM {cls.declared_name} n "
            "JOIN member ON member.input_id=n.input_id "
            "WHERE member.assignment_id=w.assignment_id AND n.session_entry_id IS NOT NULL "
            f"ORDER BY CASE n.stage WHEN {triage_stage} THEN 0 ELSE 1 END,"
            "member.ordinal,n.input_id LIMIT 1) AS context_committed_at_ms FROM w"
        )

    write_once_columns = ('sent_owner_claimed_generation', 'session_id', 'session_file')
    finalized_column = 'session_entry_id'
    required_update_columns = ('sent_owner_claimed_generation',)

    @classmethod
    def source_membership_sql(cls) -> str:
        return " UNION ALL ".join(
            member.source_membership_sql() for member in NativeInputExecution.members_with(NativeInputExecution)
        )

    def __post_init__(self):
        self.execution
        # The selected identity and the later context acknowledgement are separate
        # acquisition phases of this ORIGINAL row, never two source selectors.
        self.session_id.selected_session(self)
        self.reference

    @property
    def selected_session(self) -> NativeSessionIdentity | NoSessionRetention:
        """The original SQL session group owns its selected and absent cases."""
        return self.session_id.selected_session(self)

    @property
    def context_anchor(self) -> NativeColumn:
        return self.session_entry_id

    def require_session_identity(self) -> NativeSessionIdentity:
        """Recover the source originally attested before this row's raw writer."""
        try:
            return FieldCodec.decode(
                NativeSessionIdentity,
                {"sessionId": self.session_id, "sessionFile": self.session_file},
            )
        except MalformedRecord as error:
            raise IdentityConflict("Native input has no recorded selected session") from error

    def require_context_proof(self) -> NativeContextProof:
        """Recover the original committed acknowledgement, never manufacture it from a path."""
        self.reference.require_recorded()
        values = {item.name: self.context_value(item.name)
                  for item, _wire in FieldCodec.record_fields(NativeContextRecord)}
        return FieldCodec.construct(NativeContextRecord, values).at(
            self.require_session_identity().path,
            session_entry_start=self.session_entry_start.offset,
        )

    @property
    def execution(self) -> NativeInputExecution:
        try:
            return self.stage.from_columns(self.execution_id, self.attempt_ordinal)
        except MalformedRecord as error:
            raise IdentityConflict("Native input execution columns conflict with their stage") from error

    @classmethod
    def for_native_user(cls, db, reader, user, *, session_id):
        """The original authorized native file/input owns stage and publication.

        Viewer names and current registry owners cannot classify inherited or
        saved native history. A recorded acknowledgement additionally seals the exact
        original user entry; a precommit authorized row still owns its stage.
        """
        rows = cls.select(
            db, where="input_id=? AND session_file=? AND session_id=?",
            parameters=(user.input_id, str(reader.path), session_id),
        )
        for row in rows:
            user.require_tracked_user()
            if row.reference.recorded and not row.session_entry_id.holds(user.id):
                raise IdentityConflict("Native transcript input conflicts with its original entry")
        return rows

    @classmethod
    def transcript_projection(cls, db, reader, record, owner_lookup, *, user, session_id):
        """Borrow original stage/replies; defer rendering until SQL closes.

        No current lifecycle witness is retained. These original identities are
        frozen; TranscriptRead's original publication revision fences appends.
        """
        entry = record.entry
        originals, publications = (), ()
        if user and user.message.input_id.reported and db:
            originals = cls.for_native_user(db, reader, user, session_id=session_id)
            if entry.final_reply:
                for original in originals:
                    publications = original.published_replies(db, user, owner_lookup)
        for original in originals:
            return partial(original.execution.transcript_events, entry, user=user), publications
        # Untracked/detached history is not classified by text or native role.
        return entry.events, publications

    @classmethod
    def publication_revision(cls, root, reader, owner_lookup) -> PublishedReplyRevision:
        """Read only this native source's committed original reply relations.

        Published evidences and their input/attempt links are immutable. Their
        maximum sequence and cardinality change on publication or proof reset;
        unrelated owner activity cannot revoke a prepared page. No counter or
        projection is persisted, and the read closes before presentation work.
        """
        from .coordination_tables.executions import ExecutionRecord
        from .transcripts import TranscriptRead

        with TranscriptRead.publication_source(root) as db:
            if db is None:
                return PublishedReplyRevision(0, 0)
            return PublishedReplyRevision.read(
                db.execute(
                    "SELECT COALESCE(MAX(o.receipt_seq),0) AS through_seq, COUNT(DISTINCT n.input_id) AS inputs "
                    f"FROM {cls.declared_name} n JOIN {ExecutionRecord.declared_name} e "
                    "ON e.execution_id=n.execution_id AND e.owner_lookup=n.owner_lookup "
                    "AND e.current_attempt_ordinal=n.attempt_ordinal "
                    f"JOIN {ResponseObligation.declared_name} o ON o.execution_id=n.execution_id "
                    "WHERE n.owner_lookup=? AND n.session_file=? "
                    "AND n.session_id=? AND n.session_entry_id IS NOT NULL "
                    "AND o.receipt_seq IS NOT NULL",
                    (owner_lookup, str(reader.path), reader.session_id),
                )
            )[0]

    def published_replies(self, db, user, owner_lookup):
        """Only this original acknowledgement can lend its execution publication."""
        if self.owner_lookup != owner_lookup or not self.session_entry_id.holds(user.id):
            return ()
        return self.execution.published_replies(db, owner_lookup)

    input_id: Annotated[
        InputId,
        FrozenColumn(
            primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^0-9a-f]*'"
        ),
    ]
    stage: Annotated[
        type[NativeInputExecution], FrozenColumn(check=NativeInputExecution.sql_constraint("stage"))
    ]
    execution_id: Annotated[
        NativeColumn,
        FrozenColumn(
            references=TableReference(ExecutionRecord, ("execution_id",)), storage=TextStorage, nullable=True
        ),
    ]
    attempt_ordinal: Annotated[NativeColumn, FrozenColumn(storage=IntegerStorage, nullable=True)]
    owner_lookup: Annotated[
        ThreadLookup,
        FrozenColumn(references=TableReference(Participants, ("participant_lookup",))),
    ]
    owner_generation: Annotated[int, FrozenColumn(check="owner_generation>0")]
    owner_token_digest: Annotated[str, FrozenColumn(check="length(owner_token_digest)=64")]
    sent_owner_claimed_generation: Annotated[
        NativeClaimEpoch,
        Column(
            record_key=True,
            check="{sent_owner_claimed_generation}>0",
            storage=IntegerStorage,
            nullable=True,
        ),
    ] = field(default_factory=UnrecordedNativeClaim)
    session_id: PiSessionId | NoPiSessionId = NoPiSessionId()
    session_file: str | None = None
    session_entry_id: Annotated[NativeColumn, NativeContext(str), Column(storage=TextStorage, nullable=True)] = UnrecordedNativeColumn()
    session_entry_start: Annotated[
        NativeInputPosition,
        NativeInputLocation.position_column,
        NativeContext(RecordedNativePosition),
    ] = field(default_factory=UnrecordedNativePosition)
    request_generation: Annotated[NativeColumn, NativeContext(int), Column(storage=IntegerStorage, nullable=True)] = UnrecordedNativeColumn()
    llm_context_digest: Annotated[NativeColumn, NativeContext(str), Column(storage=TextStorage, nullable=True)] = UnrecordedNativeColumn()
    committed_at_ms: Annotated[NativeColumn, NativeContext(int), Column(storage=IntegerStorage, nullable=True)] = UnrecordedNativeColumn()
    verdict: Annotated[
        TriageDecisionRecord,
        Column(check=RecordedTriageText.sql_constraint("verdict"), storage=TextStorage, nullable=True),
    ] = AbsentTriageDecisionRecord()

    unique = (("execution_id", "attempt_ordinal"),)
    checks = (
        "(stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL) OR "
        "(stage='full' AND execution_id IS NOT NULL AND attempt_ordinal>0)",
        "(session_id IS NULL AND session_file IS NULL) OR "
        "(session_id IS NOT NULL AND length(session_id)>0 AND session_file IS NOT NULL "
        "AND length(session_file)>0 AND {sent_owner_claimed_generation} IS NOT NULL)",
        "(session_entry_id IS NULL AND session_entry_start IS NULL AND request_generation IS NULL "
        "AND llm_context_digest IS NULL "
        "AND committed_at_ms IS NULL) OR "
        "(session_entry_id IS NOT NULL AND length(session_entry_id)>0 "
        "AND session_entry_start IS NOT NULL AND request_generation>0 "
        "AND length(llm_context_digest)=64 AND committed_at_ms>0 AND session_id IS NOT NULL)",
        "stage='triage' OR verdict IS NULL",
    )

    def require_unproven(self, token_digest: str) -> None:
        """Only the original reserved capability can acquire its first proof."""
        if self.owner_token_digest != token_digest or self.reference.recorded:
            raise StaleFence("native proof belongs to a different or already settled dispatch")

    def commit_context(
        self,
        db: sqlite3.Connection,
        context: NativeContextProof,
        *,
        verdict: TriageDecisionRecord = AbsentTriageDecisionRecord(),
    ) -> None:
        """Commit the context acknowledgement once against the original selected session."""
        if context.input_id != self.input_id:
            raise StaleFence("native context belongs to another reserved input")
        self.require_session_identity().require_context(context)
        updated = self.update(
            db,
            where="input_id=? AND session_entry_id IS NULL",
            parameters=(self.input_id,),
            session_entry_id=TextNativeColumn(context.session_entry_id),
            session_entry_start=RecordedNativePosition(context.session_entry_start),
            request_generation=NumberNativeColumn(context.request_generation),
            llm_context_digest=TextNativeColumn(context.llm_context_digest),
            committed_at_ms=NumberNativeColumn(context.committed_at_ms),
            verdict=verdict,
        )
        if updated.rowcount != 1:
            raise StaleFence("native proof was previously committed")

class SelectedSlotState(DeclaredFamily, affix="Slot"):
    """Consumption is irreversible; only a matching live terminal can acknowledge it."""

    @classmethod
    def acknowledge(cls) -> type[SelectedSlotState]:
        raise SelectedToolDenied("Selected tool terminal was already acknowledged; no retry")

    @classmethod
    def require_acknowledged(cls) -> None:
        raise SelectedToolDenied("Selected tool outcome UNKNOWN; no terminal acknowledgement")


class ConsumedSlot(SelectedSlotState):
    @classmethod
    def acknowledge(cls) -> type[SelectedSlotState]:
        return AcknowledgedSlot


class AcknowledgedSlot(SelectedSlotState):
    @classmethod
    def require_acknowledged(cls) -> None:
        pass


@dataclass(frozen=True)
class SelectedToolSlot(NativeRuntimeTable, WithoutRowid):
    """The consumed slot and its live terminal acknowledgement, in one transaction store."""

    slot_id: Annotated[SlotId, FrozenColumn(primary_key=True)]
    call_id: Annotated[ToolCallId, FrozenColumn()]
    state: type[SelectedSlotState] = ConsumedSlot

    @classmethod
    def consume(
        cls, db: sqlite3.Connection, slot_id: SlotId, call_id: ToolCallId,
    ) -> None:
        if cls(slot_id, call_id).insert_if_absent(db).rowcount != 1:
            raise SelectedToolDenied("Selected tool input already consumed; outcome may be UNKNOWN")

    def require_call(self, call_id: ToolCallId) -> None:
        if self.call_id != call_id:
            raise SelectedToolDenied("Selected tool has no matching terminal acknowledgement")

    def acknowledge(self, db: sqlite3.Connection, call_id: ToolCallId) -> None:
        self.require_call(call_id)
        state = self.state.acknowledge()
        where, parameters = self._condition(
            "1", (), {"slot_id": self.slot_id, "state": self.state},
        )
        if self.update(db, where=where, parameters=parameters, state=state).rowcount != 1:
            raise SelectedToolDenied("Selected tool terminal changed; outcome UNKNOWN; no retry")

    def verify(self, call_id: ToolCallId) -> None:
        self.require_call(call_id)
        self.state.require_acknowledged()


@dataclass(frozen=True)
class CurrentNativeCursor(
    MonotoneColumns,
    StableReferenceColumns,
    NoDelete,
    NativeInputContext,
    NativeRuntimeTable,
    WithoutRowid,
):
    column_orders = (NonDecreasing(("covered_seq", "injected_seq")),)
    reference_column = 'injected_seq'
    stable_reference_columns = ('input_id',)

    @property
    def context_anchor(self) -> NativeColumn:
        return self.session_id

    wire_root_id: Annotated[
        RootId,
        FrozenColumn(
            primary_key=True,
            check="length(wire_root_id)=32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'",
        ),
    ]
    recipient_lookup: Annotated[
        ThreadLookup,
        FrozenColumn(
            primary_key=True, references=TableReference(Participants, ("participant_lookup",))
        ),
    ]
    owner_generation: Annotated[int, FrozenColumn(primary_key=True, check="owner_generation>0")]
    owner_claimed_generation: Annotated[
        int,
        FrozenColumn(record_key=True, primary_key=True, check="{owner_claimed_generation}>0"),
    ]

    @property
    def owner_identity(self):
        return OwnerGenerations(owner_lookup=self.recipient_lookup,  generation=self.owner_generation)

    covered_seq: Annotated[int, Column(check="covered_seq>=0")]
    injected_seq: Annotated[
        int, Column(check="injected_seq>=0 AND injected_seq<=covered_seq")
    ]
    input_id: Annotated[
        NativeColumn,
        Column(references=TableReference(NativeRuntimeInput, ("input_id",)), storage=TextStorage, nullable=True),
        NativeContext(InputId),
    ]
    stage: Annotated[
        NativeColumn,
        Column(
            check=NativeInputExecution.sql_constraint("stage"), storage=NativeStageStorage, nullable=True
        ),
        NativeContext(type[NativeInputExecution]),
    ]
    session_id: Annotated[
        NativeColumn, NativeContext(PiSessionId), Column(storage=TextStorage, nullable=True)
    ]
    request_generation: Annotated[NativeColumn, NativeContext(int), Column(storage=IntegerStorage, nullable=True)]

    checks = (
        "(injected_seq=0 AND input_id IS NULL AND stage IS NULL "
        "AND session_id IS NULL AND request_generation IS NULL) OR "
        "(injected_seq>0 AND input_id IS NOT NULL "
        "AND stage IS NOT NULL "
        "AND session_id IS NOT NULL AND request_generation>0)",
    )
