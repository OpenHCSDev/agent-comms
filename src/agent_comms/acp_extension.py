"""Declared Comms facts carried by the external ACP metadata envelope.

These records are shared with the paired Toad client. They describe observations;
they never authorize a native send, restore a queue, or replay an UNKNOWN input.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import ClassVar

from .acp_failure import ACPFailure, DeliveryFailure
from .agent_events import CompactionEvent, CompactionProgress
from .compaction_states import CompactionPublishedMetadata
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .goal_presentation import GoalExecution
from .goals import Goal
from .native_runtime_input import CurrentNativeCursor
from .pi_payloads import McpLiveReceipt
from .routing import MessageRoute
from .thread_identity import OwnerIdentity, ThreadIncarnation
from .transcripts import TranscriptCursor, TranscriptPage


class AgentCommsUpdate(DeclaredFamily, affix="Update"):
    """One declared fact; member identity supplies its wire discriminator."""

    def for_session(self, session_id: str) -> AgentCommsUpdate:
        return self


@dataclass(frozen=True)
class TurnStartedUpdate(AgentCommsUpdate):
    turn_id: str
    started_at: float | None
    activity: str | None
    activity_detail: str | None

    def __post_init__(self) -> None:
        if not self.turn_id or self.started_at is not None and self.started_at <= 0:
            raise ValueError("A started turn needs its actual ID and optional observed time")


@dataclass(frozen=True)
class TurnSettledUpdate(AgentCommsUpdate):
    turn_id: str | None


@dataclass(frozen=True)
class TextRouteUpdate(AgentCommsUpdate):
    route: MessageRoute | None


@dataclass(frozen=True)
class TranscriptChangedUpdate(AgentCommsUpdate):
    cursor: TranscriptCursor | None


@dataclass(frozen=True)
class InputFailedUpdate(AgentCommsUpdate):
    text: str
    failure: DeliveryFailure


@dataclass(frozen=True)
class UpdateBatch:
    updates: tuple[AgentCommsUpdate, ...]


def encode_updates(*updates: AgentCommsUpdate) -> dict:
    return {"agentComms": FieldCodec.encode(UpdateBatch(updates))}


def decode_updates(metadata: object) -> tuple[AgentCommsUpdate, ...]:
    """Decode facts once at ACP ingress, rejecting unknown kinds and fields."""
    if metadata is None:
        return ()
    if not isinstance(metadata, dict):
        raise ValueError("ACP metadata must be an object")
    if "agentComms" not in metadata:
        return ()
    extension = metadata["agentComms"]
    if not isinstance(extension, dict):
        raise ValueError("Comms metadata must be an object")
    return FieldCodec.decode(UpdateBatch, extension).updates


@dataclass(frozen=True)
class CursorScope:
    session_id: str
    wire_root_id: str
    owner: OwnerIdentity
    owner_pid: int

    def __post_init__(self):
        if (
            not self.session_id
            or len(self.wire_root_id) != 32
            or any(c not in "0123456789abcdef" for c in self.wire_root_id)
            or self.owner_pid <= 0
            or self.owner.generation <= 0
            or self.owner.incarnation.created_at <= 0
        ):
            raise ValueError("Invalid native cursor scope")

    @property
    def logical_key(self):
        return self.session_id, self.wire_root_id, self.owner.incarnation.name

    @property
    def owner_created_at(self):
        return self.owner.incarnation.created_at

    @property
    def admission_generation(self):
        return self.owner.generation


class CursorObservation(DeclaredFamily, affix="CursorObservation"):
    """Read-only history evidence, never an input/send capability."""

    @property
    @abstractmethod
    def status(self) -> str: ...

    def validate_scope(self, scope: CursorScope | None) -> None:
        pass


@dataclass(frozen=True)
class UnavailableCursorObservation(CursorObservation):
    @property
    def status(self) -> str:
        return "unavailable"


@dataclass(frozen=True)
class EmptyCursorObservation(CursorObservation):
    @property
    def status(self) -> str:
        return "none"


@dataclass(frozen=True)
class VerifiedCursorObservation(CursorObservation):
    cursor: CurrentNativeCursor

    @property
    def status(self) -> str:
        return "proven" if self.cursor.injected_seq else "coverage_only"

    def validate_scope(self, scope: CursorScope | None) -> None:
        cursor = self.cursor
        if (
            scope is None
            or cursor.wire_root_id != scope.wire_root_id
            or cursor.owner_thread != scope.owner.incarnation.name
            or cursor.owner_admission_generation != scope.owner.generation
            or not 0 <= cursor.injected_seq <= cursor.covered_seq
        ):
            raise ValueError("Cursor observation does not belong to its scope")


@dataclass(frozen=True)
class CursorEnvelope:
    scope: CursorScope | None
    revision: int
    observation: CursorObservation

    def __post_init__(self):
        if self.revision <= 0:
            raise ValueError("A cursor observation requires a positive revision")
        self.observation.validate_scope(self.scope)

    @property
    def status(self) -> str:
        return self.observation.status

    @property
    def digest(self) -> str:
        return sha256(
            json.dumps(FieldCodec.encode(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class CursorAdvancedUpdate(AgentCommsUpdate):
    envelope: CursorEnvelope
    selected_status: str | None = None

    def for_session(self, session_id: str) -> CursorAdvancedUpdate:
        if self.envelope.scope is None:
            return self
        return replace(
            self,
            envelope=replace(
                self.envelope, scope=replace(self.envelope.scope, session_id=session_id)
            ),
        )


@dataclass(frozen=True)
class QueueScope:
    session_id: str
    owner: OwnerIdentity
    owner_pid: int

    @property
    def logical_key(self):
        return self.session_id, self.owner.incarnation.name

    @property
    def owner_created_at(self):
        return self.owner.incarnation.created_at

    @property
    def admission_generation(self):
        return self.owner.generation


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


@dataclass(frozen=True)
class PendingQueueProjection(QueueProjection):
    @property
    def status(self) -> None:
        return None


@dataclass(frozen=True)
class UnavailableQueueProjection(QueueProjection):
    @property
    def status(self) -> str:
        return "unavailable"


@dataclass(frozen=True)
class AvailableQueueProjection(QueueProjection):
    items: tuple[QueueItem, ...] = ()
    restored: tuple[QueueItem, ...] = ()

    @property
    def status(self) -> str:
        return "available"


@dataclass(frozen=True)
class QueueChangedUpdate(AgentCommsUpdate):
    scope: QueueScope | None
    revision: int
    projection: QueueProjection

    def for_session(self, session_id: str) -> QueueChangedUpdate:
        return (
            self
            if self.scope is None
            else replace(self, scope=replace(self.scope, session_id=session_id))
        )

    @property
    def status(self) -> str | None:
        return self.projection.status

    @property
    def digest(self) -> str:
        return sha256(
            json.dumps(FieldCodec.encode(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class InputStartedUpdate(AgentCommsUpdate):
    input_id: str | None
    text: str | None
    scope: QueueScope | None
    revision: int | None

    def for_session(self, session_id: str) -> InputStartedUpdate:
        return (
            self
            if self.scope is None
            else replace(self, scope=replace(self.scope, session_id=session_id))
        )


@dataclass(frozen=True)
class ContextUsage:
    used: int
    size: int


@dataclass(frozen=True)
class CoordinationChangedUpdate(AgentCommsUpdate):
    thread: ThreadIncarnation
    wire_root: str
    owner_pid: int
    worktree: str
    model: str | None
    thinking_level: str | None
    title: str
    context_usage: ContextUsage | None

    @property
    def persistence(self) -> str:
        return "shared on-disk wire"

    @property
    def transport(self) -> str:
        return "per-session stdio ACP"


@dataclass(frozen=True)
class GoalChangedUpdate(AgentCommsUpdate):
    goal: Goal | None
    execution: GoalExecution | None

    def __post_init__(self):
        if self.execution is not None and (
            self.goal is None or self.execution.goal_id != self.goal.id
        ):
            raise ValueError("Goal execution identity does not match its declaration")


@dataclass(frozen=True)
class CompactionChangedUpdate(AgentCommsUpdate):
    event: CompactionEvent | CompactionProgress


@dataclass(frozen=True)
class CompactionCommittedUpdate(AgentCommsUpdate):
    commit_id: str
    summary: str


@dataclass(frozen=True)
class TranscriptSnapshotUpdate(AgentCommsUpdate):
    page: TranscriptPage


@dataclass(frozen=True)
class InputDeliveryChangedUpdate(AgentCommsUpdate):
    input_id: str | None = None


@dataclass(frozen=True)
class CompactionPublishedUpdate(AgentCommsUpdate):
    publication: CompactionPublishedMetadata


@dataclass(frozen=True)
class McpClientReceiptUpdate(AgentCommsUpdate):
    turn_id: str
    receipt: McpLiveReceipt


class CommsRequest(DeclaredFamily, affix="Request"):
    """One owner command decoded at ACP ingress, never a bag of control flags."""

    @property
    def draft_text(self) -> str | None:
        return None


@dataclass(frozen=True)
class PromptRequest(CommsRequest):
    user_text: str | None = None
    defer_display: bool = False

    @property
    def draft_text(self) -> str | None:
        return self.user_text

    @property
    @abstractmethod
    def delivery(self) -> str: ...


class QueuePromptRequest(PromptRequest):
    @property
    def delivery(self) -> str:
        return "queue"


class SteerPromptRequest(PromptRequest):
    @property
    def delivery(self) -> str:
        return "steer"


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
        raise ValueError("ACP metadata must be an object")
    extension = expanded.get("agentComms", metadata.get("agentComms"))
    if extension is None:
        return QueuePromptRequest()
    if not isinstance(extension, dict) or set(extension) != {"request"}:
        raise ValueError("Comms metadata requires one declared request")
    return FieldCodec.decode(CommsRequest, extension["request"])


@dataclass(frozen=True)
class RequestFailedUpdate(AgentCommsUpdate):
    failure: ACPFailure
