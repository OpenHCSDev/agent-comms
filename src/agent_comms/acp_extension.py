"""Declared Comms facts carried by the external ACP metadata envelope.

These records are shared with the paired Toad client. They describe observations;
they never authorize a native send, restore a queue, or replay an UNKNOWN input.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, Callable, ClassVar


from .acp_failure import (
    ACPFailure,
    BackendDeliveryFailure,
    DeliveryFailure,
    FailureSource,
    UnrecordedFailureSource,
)
from .agent_backend import InputId, SendNow, TurnInbox
from .agent_events import CompactionEvent
from .child_process import ProcessIdentity
from .context_reading import ContextReading
from .declared_family import DeclaredFamily
from .external_transcript import DraftRow
from .field_codec import (
    AcceptsNull,
    ExternalJsonShape,
    FieldCodec,
    Id128Text,
    MalformedRecord,
    WireValue,
    WireName,
    WireOmitDefault,
)
from .goal_presentation import GoalSnapshot, GoalExecution
from .input_attempt import InputAttempt
from .input_origin import HumanInputOrigin, InputOrigin, UnattributedInputOrigin
from .native_runtime_input import CurrentNativeCursor
from .owner_protocol import IncompatibleOwner
from .pi_payloads import McpLiveAcknowledgement
from .routing import MessageRoute
from .thread_identity import ClaimedIdentity, ThreadIncarnation
from .tool_result import DeclaredReply
from .transcripts import TranscriptCursor, TranscriptPage, TranscriptReadIdentity
from .turn_context import ContextManifest
from .turn_lease import TurnState
from .working_memory_labels import ModelLabel

if TYPE_CHECKING:
    from acp.schema import AgentMessageChunk

    from .input_drain import InputDrain


class AgentCommsUpdate(DeclaredFamily, affix="Update"):
    """One declared fact; member identity supplies its wire discriminator."""

    def for_session(self, session_id: str) -> AgentCommsUpdate:
        return self

    def matches_turn(self, turn_id: str) -> bool:
        """Facts without a turn identity apply throughout their session."""
        return True

    def acp_chunk(self) -> AgentMessageChunk:
        """Carry this declared observation in its original empty ACP envelope."""
        from acp.schema import AgentMessageChunk, TextContentBlock

        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=""),
            field_meta=encode_updates(self),
        )


@dataclass(frozen=True)
class ContextAnnotatedUpdate(AgentCommsUpdate):
    context: ContextManifest
    labels: tuple[ModelLabel, ...]


@dataclass(frozen=True)
class TurnChangedUpdate(AgentCommsUpdate):
    state: TurnState


@dataclass(frozen=True)
class TextRouteUpdate(AgentCommsUpdate):
    route: MessageRoute | None


@dataclass(frozen=True)
class TranscriptChangedUpdate(AgentCommsUpdate):
    cursor: TranscriptCursor | None


class ClaimBlockedFailure(BackendDeliveryFailure):
    def present(self, receiver, body: str) -> None:
        receiver.delivery_blocked(self.description, body)


@dataclass(frozen=True)
class InputFailedUpdate(AgentCommsUpdate):
    text: str
    failure: DeliveryFailure


@dataclass(frozen=True)
class UpdateBatch:
    updates: tuple[AgentCommsUpdate, ...]


@dataclass(frozen=True)
class UpdateMetadata:
    """An ACP ``_meta`` object carrying one batch of Comms updates."""

    batch: Annotated[UpdateBatch, WireName("agentComms")]

    @classmethod
    def of(cls, *updates: AgentCommsUpdate) -> UpdateMetadata:
        return cls(UpdateBatch(updates))


@dataclass(frozen=True)
class GoalSnapshotReply(DeclaredReply, GoalSnapshot):
    """A goal request's reply: the owner's goal snapshot and its turn state."""

    execution: Annotated[GoalExecution, WireName("goalExecution")]
    meta: Annotated[UpdateMetadata, WireName("_meta")]


def encode_updates(*updates: AgentCommsUpdate) -> dict:
    return {"agentComms": FieldCodec.encode(UpdateBatch(updates))}


def decode_updates(metadata: object) -> tuple[AgentCommsUpdate, ...]:
    """Decode facts once at ACP ingress, rejecting unknown kinds and fields."""
    if metadata is None:
        return ()
    if not isinstance(metadata, dict):
        raise MalformedRecord("ACP metadata must be an object")
    if "agentComms" not in metadata:
        return ()
    extension = metadata["agentComms"]
    if not isinstance(extension, dict):
        raise ValueError("Comms metadata must be an object")
    return FieldCodec.decode(UpdateBatch, extension).updates


class AttachmentScope(AcceptsNull, ExternalJsonShape, WireValue):
    """One decoded attachment; JSON null is an absent attachment, not Python None."""

    @classmethod
    def from_wire(cls, data):
        return cls.empty_scope() if data is None else FieldCodec.decode_record(cls.record_type(), data)

    @classmethod
    @abstractmethod
    def empty_scope(cls): ...

    @classmethod
    @abstractmethod
    def record_type(cls): ...

    @classmethod
    def schema(cls):
        return {"anyOf": [{"type": "null"}, FieldCodec.record_schema(cls.record_type())]}

    def to_wire(self):
        return None

    def for_session(self, session_id: str):
        return self

    def matches_session(self, session_id: str) -> bool:
        return False

    def foreign_session(self, session_id: str) -> bool:
        return False

    def relation(self, other: AttachmentScope) -> AttachmentRelation:
        return AttachmentRelation()

    def relation_from(self, original: LiveAttachmentScope) -> AttachmentRelation:
        return AttachmentRelation()

    def current_for(self, other: AttachmentScope) -> bool:
        return self.relation(other).current

    def same_request(self, other: AttachmentScope) -> bool:
        return self == other or self.current_for(other)

    def require_live(self):
        raise ValueError('Attachment has no live local owner scope.')


class LiveAttachmentScope(AttachmentScope):
    """Logical ACP attachment and executor evidence have separate identities."""
    session_id: str
    claimed_identity: ClaimedIdentity
    owner: ProcessIdentity

    @property
    @abstractmethod
    def logical_key(self) -> tuple[str, ...]: ...

    @property
    def owner_created_at(self):
        return self.claimed_identity.incarnation.require_recorded().created_at

    @property
    def claimed_generation(self):
        return self.claimed_identity.claimed_generation

    def to_wire(self):
        return FieldCodec.encode_record(self)

    def for_session(self, session_id: str):
        return replace(self, session_id=session_id)

    def matches_session(self, session_id: str) -> bool:
        return self.session_id == session_id

    def foreign_session(self, session_id: str) -> bool:
        return self.session_id != session_id

    def require_live(self):
        return self

    def relation(self, other: AttachmentScope) -> AttachmentRelation:
        return other.relation_from(self)

    def relation_from(self, original: LiveAttachmentScope) -> AttachmentRelation:
        return ObservedAttachmentRelation(original, self)


class AttachmentRelation:
    """Absent evidence cannot identify a common live attachment."""
    foreign = True
    same_incarnation = False
    ambiguous = True
    newer = False
    older = False
    current = False


@dataclass(frozen=True)
class ObservedAttachmentRelation(AttachmentRelation):
    """Compare original scope witnesses once; no state or alias registry."""
    original: AttachmentScope
    received: AttachmentScope

    @property
    def foreign(self):
        return self.original.logical_key != self.received.logical_key

    @property
    def same_incarnation(self):
        return (not self.foreign and self.original.owner_created_at == self.received.owner_created_at)

    @property
    def ambiguous(self):
        return (not self.same_incarnation or
                (self.original.claimed_generation == self.received.claimed_generation
                 and self.original.owner != self.received.owner))

    @property
    def newer(self):
        return self.same_incarnation and self.received.claimed_generation > self.original.claimed_generation

    @property
    def older(self):
        return self.same_incarnation and self.received.claimed_generation < self.original.claimed_generation

    @property
    def current(self):
        return (self.same_incarnation
                and self.original.claimed_generation == self.received.claimed_generation
                and self.original.owner == self.received.owner)


class CursorScope(AttachmentScope):
    @classmethod
    def empty_scope(cls):
        return NoCursorScope()

    @classmethod
    def record_type(cls):
        return LiveCursorScope


@dataclass(frozen=True)
class NoCursorScope(CursorScope):
    pass


@dataclass(frozen=True)
class LiveCursorScope(CursorScope, LiveAttachmentScope):
    session_id: str
    wire_root_id: str
    claimed_identity: ClaimedIdentity
    owner: ProcessIdentity

    def __post_init__(self):
        Id128Text.decode(self.wire_root_id)
        self.claimed_identity.incarnation.require_recorded()
        if (
            not self.session_id
            or self.claimed_identity.claimed_generation <= 0
        ):
            raise ValueError("Invalid native cursor scope")

    @property
    def logical_key(self):
        return self.session_id, self.wire_root_id


class CursorObservation(DeclaredFamily, affix="CursorObservation"):
    """Read-only history evidence, never an input/send capability."""

    @property
    @abstractmethod
    def status(self) -> str: ...

    def validate_scope(self, scope: CursorScope) -> None:
        pass

    @property
    def needs_refresh(self) -> bool:
        return False


@dataclass(frozen=True)
class UnavailableCursorObservation(CursorObservation):
    @property
    def needs_refresh(self) -> bool:
        return True

    @property
    def status(self) -> str:
        return "unavailable"


@dataclass(frozen=True)
class EmptyCursorObservation(CursorObservation):
    @property
    def needs_refresh(self) -> bool:
        # Absence of a durable projection does not settle an original proof.
        return True

    @property
    def status(self) -> str:
        return "none"


@dataclass(frozen=True)
class VerifiedCursorObservation(CursorObservation):
    cursor: CurrentNativeCursor

    @property
    def status(self) -> str:
        return "proven" if self.cursor.injected_seq else "coverage_only"

    def validate_scope(self, scope: CursorScope) -> None:
        scope = scope.require_live()
        cursor = self.cursor
        if (
            cursor.wire_root_id != scope.wire_root_id
            or cursor.recipient_lookup != scope.claimed_identity.incarnation.lookup
            or cursor.owner_claimed_generation != scope.claimed_generation
            or not 0 <= cursor.injected_seq <= cursor.covered_seq
        ):
            raise ValueError("Cursor observation does not belong to its scope")


@dataclass(frozen=True)
class CursorEnvelope:
    scope: CursorScope
    revision: int
    observation: CursorObservation

    def __post_init__(self):
        if self.revision <= 0:
            raise ValueError("A cursor observation requires a positive revision")
        self.observation.validate_scope(self.scope)

    @property
    def status(self) -> str:
        return self.observation.status

    def same_observation(self, other: CursorEnvelope) -> bool:
        """Publication equality excludes only the local observation revision."""
        return self.scope == other.scope and self.observation == other.observation

    @property
    def digest(self) -> str:
        return FieldCodec.digest(self)


@dataclass(frozen=True)
class CursorAdvancedUpdate(AgentCommsUpdate):
    envelope: CursorEnvelope
    selected_status: str | None = None

    def for_session(self, session_id: str) -> CursorAdvancedUpdate:
        return replace(self, envelope=replace(
            self.envelope, scope=self.envelope.scope.for_session(session_id)))


class QueueScope(AttachmentScope):
    @classmethod
    def empty_scope(cls):
        return NoQueueScope()

    @classmethod
    def record_type(cls):
        return LiveQueueScope

    def projection(self, queued, restored) -> QueueProjection:
        return UnavailableQueueProjection()

    def validate_scope(self, projection: QueueProjection) -> None:
        projection.without_scope()

    def start_revision(self, inputs: InputDrain, session_id: str) -> int | None:
        return None

    def accepts_request(self, projection: QueueProjection) -> bool:
        return True

    def unidentified_start(self, input_id: str | None) -> bool:
        return input_id is None


@dataclass(frozen=True)
class NoQueueScope(QueueScope):
    pass


@dataclass(frozen=True)
class LiveQueueScope(QueueScope, LiveAttachmentScope):
    session_id: str
    claimed_identity: ClaimedIdentity
    owner: ProcessIdentity

    @property
    def logical_key(self):
        return (self.session_id,)

    def projection(self, queued, restored) -> QueueProjection:
        items = tuple(row for item in queued for row in item.queue_items(self))
        previous = tuple(row for item in restored for row in item.queue_items(self))
        rows = items + previous
        try:
            sizes = [len(row.text.encode("utf-8")) for row in rows]
        except UnicodeError:
            return UnavailableQueueProjection()
        if len(rows) > 32 or any(size > 4096 for size in sizes) or sum(sizes) > 65536:
            return UnavailableQueueProjection()
        return AvailableQueueProjection(items, previous)

    def validate_scope(self, projection: QueueProjection) -> None:
        pass

    def start_revision(self, inputs: InputDrain, session_id: str) -> int:
        return inputs.next_revision(session_id)

    def accepts_request(self, projection: QueueProjection) -> bool:
        return projection.status == 'available'

    def unidentified_start(self, input_id: str | None) -> bool:
        return False


@dataclass(frozen=True)
class QueueItem:
    input_id: str
    text: str


class QueueProjection(DeclaredFamily, affix="QueueProjection"):
    items: ClassVar[tuple[QueueItem, ...]] = ()
    restored: ClassVar[tuple[QueueItem, ...]] = ()

    @property
    @abstractmethod
    def status(self) -> str | None: ...

    @abstractmethod
    def feedback(self, supported: bool) -> str: ...

    def without_scope(self) -> None:
        """Do not offer queue access while its state is missing or still loading."""

    def capture_human_input(self, comms, acquire_scope: Callable[[], QueueScope]) -> HumanInputOrigin:
        """Refuse input until the queue's current state has been read."""
        raise ValueError(self.feedback(True))


@dataclass(frozen=True)
class PendingQueueProjection(QueueProjection):
    @property
    def status(self) -> None:
        return None

    def feedback(self, supported: bool) -> str:
        return "Checking input queue…" if supported else ""


@dataclass(frozen=True)
class UnavailableQueueProjection(QueueProjection):
    @property
    def status(self) -> str:
        return "unavailable"

    def feedback(self, supported: bool) -> str:
        return "Remote queue unavailable (input status unchanged)"


@dataclass(frozen=True)
class HistoryOnlyQueueProjection(UnavailableQueueProjection):
    """The owner runs another Core, so this client cannot queue its input."""

    owner: IncompatibleOwner

    def feedback(self, supported: bool) -> str:
        return self.owner.line()


@dataclass(frozen=True)
class AvailableQueueProjection(QueueProjection):
    items: tuple[QueueItem, ...] = ()
    restored: tuple[QueueItem, ...] = ()

    @property
    def status(self) -> str:
        return "available"

    def feedback(self, supported: bool) -> str:
        return ""

    def without_scope(self) -> None:
        raise ValueError('Available input queue has no original attachment scope.')

    def capture_human_input(self, comms, acquire_scope: Callable[[], QueueScope]) -> HumanInputOrigin:
        return HumanInputOrigin.capture(comms, acquire_scope().claimed_identity)


@dataclass(frozen=True)
class QueueChangedUpdate(AgentCommsUpdate):
    scope: QueueScope
    revision: int
    projection: QueueProjection

    def __post_init__(self):
        self.scope.validate_scope(self.projection)

    def for_session(self, session_id: str) -> QueueChangedUpdate:
        return replace(self, scope=self.scope.for_session(session_id))

    @property
    def status(self) -> str | None:
        return self.projection.status

    @property
    def digest(self) -> str:
        return FieldCodec.digest(self)


@dataclass(frozen=True)
class InputStartedUpdate(AgentCommsUpdate):
    input_id: str | None
    text: str | None
    scope: QueueScope
    revision: int | None
    native_id: str | None

    def for_session(self, session_id: str) -> InputStartedUpdate:
        return replace(self, scope=self.scope.for_session(session_id))


@dataclass(frozen=True)
class CoordinationChangedUpdate(AgentCommsUpdate):
    thread: ThreadIncarnation
    wire_root: str
    owner: ProcessIdentity
    worktree: str
    model: str
    thinking_level: str | None
    title: str
    context_usage: ContextReading

    @property
    def status_label(self) -> str:
        return " · ".join((self.model, *self.context_usage.label_parts()))

    @property
    def persistence(self) -> str:
        return "shared on-disk wire"

    @property
    def transport(self) -> str:
        return "per-session stdio ACP"


@dataclass(frozen=True)
class GoalChangedUpdate(GoalSnapshot, AgentCommsUpdate):
    """The goal snapshot after a change, announced to the session."""


@dataclass(frozen=True)
class CompactionChangedUpdate(AgentCommsUpdate):
    event: CompactionEvent


@dataclass(frozen=True)
class CompactionCommittedUpdate(AgentCommsUpdate):
    """The /compact reply: the runtime placed this summary before ``first_kept``."""

    summary: str
    first_kept: str | None


@dataclass(frozen=True)
class TranscriptDraftUpdate(AgentCommsUpdate):
    """Current draft beside an unchanged, already published transcript page."""

    identity: TranscriptReadIdentity
    draft: DraftRow | None


@dataclass(frozen=True)
class TranscriptSnapshotUpdate(AgentCommsUpdate):
    page: TranscriptPage
    identity: TranscriptReadIdentity
    draft: DraftRow | None = None

    @classmethod
    def from_read(cls, read):
        page, draft = read.read_snapshot()
        return cls(page, read.identity, draft.external_row())

    @classmethod
    def capture(cls, transcripts, name, *, historical_source=None):
        """Publish the page together with the source witness that read it."""
        read = transcripts.capture_page_read(name, historical_source=historical_source)
        return cls.from_read(read)


@dataclass(frozen=True)
class InputDeliveryChangedUpdate(AgentCommsUpdate):
    input_id: str | None = None


@dataclass(frozen=True)
class McpClientAcknowledgementUpdate(AgentCommsUpdate, declared_name="mcp_client_receipt"):
    turn_id: str
    acknowledgement: Annotated[McpLiveAcknowledgement, WireName("receipt")]

    def matches_turn(self, turn_id: str) -> bool:
        return self.turn_id == turn_id


class CommsRequest(DeclaredFamily, affix="Request"):
    """One owner command decoded at ACP ingress, never a bag of control flags."""

    @property
    def draft_text(self) -> str | None:
        return None


@dataclass(frozen=True)
class PromptRequest(CommsRequest):
    user_text: str | None = None
    defer_display: bool = False
    input_id: Annotated[str, Id128Text] = field(default_factory=Id128Text.mint)
    origin: Annotated[InputOrigin, WireOmitDefault()] = field(
        default_factory=UnattributedInputOrigin, kw_only=True
    )

    def __post_init__(self):
        Id128Text.decode(self.input_id)

    @property
    def draft_text(self) -> str | None:
        return self.user_text

    @abstractmethod
    def enqueue_control(self, inbox: TurnInbox, input_id: str) -> None: ...

    async def publish_acceptance(self, inputs: InputDrain, session_id: str) -> None:
        await inputs.emit_queue_state(session_id)


class QueuePromptRequest(PromptRequest):
    def enqueue_control(self, inbox: TurnInbox, input_id: str) -> None:
        pass


class SteerPromptRequest(PromptRequest):
    def enqueue_control(self, inbox: TurnInbox, input_id: str) -> None:
        inbox.put_nowait(SendNow((InputId(input_id),)))


@dataclass(frozen=True)
class ClearQueueRequest(CommsRequest):
    pass


@dataclass(frozen=True)
class SendNowRequest(CommsRequest):
    pass


@dataclass(frozen=True)
class CompactRequest(CommsRequest):
    instructions: str | None = None

    def __post_init__(self):
        if self.instructions is not None and len(self.instructions.strip()) > 2000:
            raise ValueError("Compaction instructions are too long")


@dataclass(frozen=True)
class SelectedWriteRequest(CommsRequest):
    source_seq: int
    source_message_id: str
    resource: str
    contents: str

    def __post_init__(self):
        if self.source_seq <= 0 or not self.source_message_id or not self.resource:
            raise ValueError("Selected write requires actual source and resource identity")


@dataclass(frozen=True)
class SelectedWriteAcceptedUpdate(AgentCommsUpdate):
    operation_id: str
    source_seq: int
    claim_id: str


def encode_request(request: CommsRequest) -> dict:
    return {"agentComms": {"request": FieldCodec.encode(request)}}


def decode_request(metadata: object = None, **expanded) -> CommsRequest:
    """ACP SDK keyword expansion is normalized here, once, before dispatch."""
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise MalformedRecord("ACP metadata must be an object")
    extension = expanded.get("agentComms", metadata.get("agentComms"))
    if extension is None:
        return QueuePromptRequest()
    if not isinstance(extension, dict) or set(extension) != {"request"}:
        raise MalformedRecord("Comms metadata requires one declared request")
    return FieldCodec.decode(CommsRequest, extension["request"])


@dataclass(frozen=True)
class RequestFailedUpdate(AgentCommsUpdate):
    failure: ACPFailure
    source: FailureSource = UnrecordedFailureSource()


@dataclass(frozen=True)
class PromptCancelledUpdate(AgentCommsUpdate):
    input_state: type[InputAttempt] | None

    @property
    def feedback(self):
        return (self.input_state or InputAttempt).cancellation_feedback
