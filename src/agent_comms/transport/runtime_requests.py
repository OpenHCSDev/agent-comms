from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Annotated,
    ClassVar,
)

from acp.schema import PromptRequest

from agent_comms.comms import Comms
from agent_comms.context_store import ContextManifestReference
from agent_comms.coordination.annotation_correction import CorrectsAnnotation
from agent_comms.execution.turn_context import (
    DispatchRequest,
    PreviewProvenance,
    Provenance,
    RecordedContextTurn,
    SegmentManifest,
)
from agent_comms.acp_extension import GoalSnapshotReply, UpdateMetadata
from agent_comms.nominal.field_codec import FieldCodec, GoalId, WireName
from agent_comms.planning.goal_actions import (
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    OwnerInvocable,
    SetGoalAction,
)
from agent_comms.planning.goal_scope import AllGoals, OneGoal
from agent_comms.planning.goals import Goal, GoalDeclaration, GoalReply, GoalRevision
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
from agent_comms.task_attachments.owner_notices import GoalWakeNotice
from agent_comms.transport.tool_result import DeclaredReply
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

    action: ClassVar[type[QueueAction]]

    def __init_subclass__(cls, *, action: type[QueueAction], **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls.action = action

    @classmethod
    def serving(cls, action: type[QueueAction]) -> type[QueueActionRuntimeRequest]:
        for member in cls.members_with(cls):
            if member.action is action:
                return member
        raise KeyError(action)

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

    def held_by_bus(self, comms: Comms, thread: Thread) -> object:
        return FieldCodec.encode(comms.goals.recorded_history(thread.name, self.goal_id))


class HeldGoalChange(AttachedResultRuntimeRequest, declared_name=None):
    """An owner's UI change to the goal the bus holds for a CLI: Core writes it as the owner,
    fenced to the incarnation the UI read, then wakes the CLI through its inbox while the goal
    is active, as a nudge does."""

    @abstractmethod
    def held_action(self, thread: Thread) -> GoalAction: ...

    @abstractmethod
    def held_reply(self, comms: Comms, goal: GoalDeclaration) -> DeclaredReply: ...

    def held_by_bus(self, comms: Comms, thread: Thread) -> object:
        goal = comms.goals.update_born_goal(
            thread.incarnation, self.held_action(thread), actor=OwnerInvocable)
        GoalWakeNotice.wake(comms.messaging, comms.registry.require(thread.name))
        return self.held_reply(comms, goal).to_wire()


class GoalSnapshotResultRuntimeRequest(HeldGoalChange, declared_name=None):
    def held_reply(self, comms: Comms, goal: GoalDeclaration) -> DeclaredReply:
        return GoalSnapshotReply.from_snapshot(comms.goals.goal_snapshot(self.thread), UpdateMetadata.of())


@dataclass(frozen=True, kw_only=True)
class GoalRevisionRuntimeRequest(AttachedResultRuntimeRequest, declared_name=None):
    goal_id: GoalId
    expected_revision: int

    def held_precondition(self, thread: Thread) -> GoalPrecondition:
        """The goal revision the UI saw, as the native owner fences it."""
        goal = thread.goal.require_checkpoint(GoalRevision(self.goal_id, self.expected_revision))
        return GoalPrecondition(goal_id=OneGoal(self.goal_id), expected_checkpoint=goal.captured)


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
    def held_action(self, thread: Thread) -> GoalAction:
        return EditGoalAction(text=self.text, expect=self.held_precondition(thread))


@dataclass(frozen=True, kw_only=True)
class UpdateGoalRuntimeRequest(GoalRevisionRuntimeRequest, GoalSnapshotResultRuntimeRequest):
    status: type[GoalAction]

    def held_action(self, thread: Thread) -> GoalAction:
        self.status.require_owner_control()
        return self.status(expect=self.held_precondition(thread))


@dataclass(frozen=True, kw_only=True)
class RetryGoalRuntimeRequest(GoalRevisionRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class SetGoalRuntimeRequest(GoalTextRuntimeRequest, HeldGoalChange):
    def held_action(self, thread: Thread) -> GoalAction:
        return SetGoalAction(text=self.text)

    def held_reply(self, comms: Comms, goal: GoalDeclaration) -> DeclaredReply:
        return GoalReply(goal.require())


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
