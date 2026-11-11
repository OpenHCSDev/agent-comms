from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import (
    Annotated,
    ClassVar,
)

from acp.schema import PromptRequest

from agent_comms.context_store import ContextManifestReference
from agent_comms.coordination.annotation_correction import CorrectsAnnotation
from agent_comms.execution.turn_context import (
    DispatchRequest,
    PreviewProvenance,
    Provenance,
    RecordedContextTurn,
    SegmentManifest,
)
from agent_comms.nominal.field_codec import GoalId, WireName
from agent_comms.planning.goal_actions import GoalAction
from agent_comms.planning.goal_scope import AllGoals, OneGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.transport.owner_connection import NoControllerToken, OwnerConnection
from agent_comms.transport.runtime_request import (
    AttachedResultRuntimeRequest,
    AttachedRuntimeRequest,
)
from agent_comms.transport.tools.sidebar import (
    ClearQueueQueueAction,
    QueueAction,
    SendNowQueueAction,
)
from agent_comms.working_memory.working_memory_labels import ClassifierVersion, ModelLabel
from agent_comms.working_memory.working_memory_questions import SpanAnswer


@dataclass(frozen=True, kw_only=True)
class SubscribeRuntimeRequest(AttachedRuntimeRequest):
    pass


class FreshSubscribeRuntimeRequest(SubscribeRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class PromptRuntimeRequest(AttachedResultRuntimeRequest):
    request: PromptRequest
    controller_token: Annotated[str | NoControllerToken, WireName("controllerToken")] = (
        NoControllerToken()
    )

    @classmethod
    def proxy_payload(
        cls, owner: ThreadIncarnation, controller_token: str | NoControllerToken,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        return super().proxy_payload(
            owner, controller_token, {"controllerToken": controller_token, **parameters}
        )


@dataclass(frozen=True, kw_only=True)
class CancelRuntimeRequest(AttachedResultRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class QueueActionRuntimeRequest(AttachedResultRuntimeRequest, declared_name=None):
    """A queue action any client asks of a thread's owner, attached over ACP or not. Each
    member names the declared QueueAction it serves."""

    serves: ClassVar[dict[type[QueueAction], type[QueueActionRuntimeRequest]]] = {}

    def __init_subclass__(cls, *, action: type[QueueAction], **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        QueueActionRuntimeRequest.serves[action] = cls

    @classmethod
    def serving(cls, action: type[QueueAction]) -> type[QueueActionRuntimeRequest]:
        return cls.serves[action]

    @classmethod
    def at_owner(cls, root: Path, thread: Thread) -> None:
        """Ask the thread's live owner process to apply this action to its queued inputs."""
        OwnerConnection.exchange_at(root, thread, cls(thread=thread.name))


@dataclass(frozen=True, kw_only=True)
class SendNowRuntimeRequest(QueueActionRuntimeRequest, action=SendNowQueueAction):
    pass


@dataclass(frozen=True, kw_only=True)
class ClearQueueRuntimeRequest(QueueActionRuntimeRequest, action=ClearQueueQueueAction):
    pass


@dataclass(frozen=True, kw_only=True)
class SetConfigOptionRuntimeRequest(AttachedResultRuntimeRequest):
    config_id: str
    value: str | bool


@dataclass(frozen=True, kw_only=True)
class CompactRuntimeRequest(AttachedResultRuntimeRequest):
    instructions: str | None = None


@dataclass(frozen=True, kw_only=True)
class DismissHistoricalInputsRuntimeRequest(AttachedResultRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class GoalHistoryRuntimeRequest(AttachedResultRuntimeRequest):
    goal_id: OneGoal | AllGoals = AllGoals()


class GoalSnapshotResultRuntimeRequest(
    AttachedResultRuntimeRequest,
    declared_name=None,
):
    pass


@dataclass(frozen=True, kw_only=True)
class GoalRevisionRuntimeRequest(AttachedResultRuntimeRequest, declared_name=None):
    goal_id: GoalId
    expected_revision: int


@dataclass(frozen=True, kw_only=True)
class GoalTextRuntimeRequest(AttachedResultRuntimeRequest, declared_name=None):
    text: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("A goal requires text.")


@dataclass(frozen=True, kw_only=True)
class EditGoalRuntimeRequest(
    GoalTextRuntimeRequest, GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest
):
    pass


@dataclass(frozen=True, kw_only=True)
class UpdateGoalRuntimeRequest(GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest):
    status: type[GoalAction]


@dataclass(frozen=True, kw_only=True)
class RetryGoalRuntimeRequest(GoalRevisionRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class SetGoalRuntimeRequest(GoalTextRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class ContextRuntimeRequest(AttachedResultRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class ContextSourceRuntimeRequest(ContextRuntimeRequest):
    observation: PreviewProvenance
    segment: int
    source: Provenance


@dataclass(frozen=True, kw_only=True)
class ContextCoreRuntimeRequest(AttachedResultRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class ContextCoreSourceRuntimeRequest(ContextCoreRuntimeRequest):
    owner: ThreadIncarnation
    segment: int
    manifest: SegmentManifest
    source: Provenance


@dataclass(frozen=True, kw_only=True)
class RecordedContextRuntimeRequest(AttachedResultRuntimeRequest, declared_name=None):
    turn: RecordedContextTurn
    request_id: DispatchRequest
    record: ContextManifestReference
    segment: int


@dataclass(frozen=True, kw_only=True)
class ContextReferenceRuntimeRequest(RecordedContextRuntimeRequest):
    source: Provenance


@dataclass(frozen=True, kw_only=True)
class ContextRecordedSegmentRuntimeRequest(RecordedContextRuntimeRequest):
    contributors: tuple[int, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationsRuntimeRequest(ContextRecordedSegmentRuntimeRequest):
    classifier: ClassifierVersion


@dataclass(frozen=True, kw_only=True)
class ContextAnnotationCorrectionRuntimeRequest(CorrectsAnnotation, ContextRecordedSegmentRuntimeRequest):
    label: ModelLabel
    answer: type[SpanAnswer]
    worktree: Checkout
