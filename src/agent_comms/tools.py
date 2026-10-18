"""Named external tool requests; declarations own schema, context and behavior."""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping
from pathlib import Path
from dataclasses import MISSING, dataclass, field, fields, replace
from typing import TYPE_CHECKING, ClassVar, Literal, NoReturn, TextIO, Type

from pydantic import JsonValue

from .pi_vocabulary import ServiceTier, ThinkingLevel, ThinkingLevelField
from .cli_commands import (
    ArchiveCliCommand, CliCommand, DeleteViewCliCommand, ForkCliCommand,
    PinChannelCliCommand, PinThreadCliCommand, StartCliCommand, StopCliCommand,
    ThreadArchivedResult, ThreadCreatedResult, ThreadStoppedResult, ThreadsCliCommand,
    _json_object, option,
)
from .thread_execution import (
    ArchiveOwnerControl, NativeOwnerCommand, OwnerLifecycleControl,
    StartOwnerControl, ThreadCommandAvailability,
)
from .channel_management import TagAction, TagDisposition, KeepThreadsTagDisposition
from .channel_targets import ChannelTarget
from .channels import Channel, SavedView, ViewKind, ViewMatch, ViewPredicate
from .declared_family import DeclaredFamily
from .display_order import ChannelSort, ThreadSort
from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord
from .launch_runtime import LaunchRuntime
from .goal_actions import (
    ActiveGoalAction,
    CreateProblemGoalAction,
    DeliveredOrderGoalAction,
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    ModelInvocable,
    OrderGoalAction,
    SetGoalAction,
)
from .goal_history import RecordedGoalRevision
from .goal_waits import StandbyReview, StandbyReviewSummary
from .input_attempt import UnresolvedInput
from .goal_presentation import GoalSnapshot, ThreadGoalExecution
from .goal_states import ActiveGoal
from .goals import Goal, OrderCheck
from .message_reference import MessageReference
from .messages import Message
from .owner_lifecycle import OwnerStartResult
from .presentation import ChannelView
from .relationships import Collaboration, GoalDerivedContact, GoalMentionDiagnostic
from .restart_queue import QueuedRestart, cancel as cancel_restart, enqueue as enqueue_restart
from .restart_queue import status as restart_status
from .thread_management import RenameThreadResult
from .transcript_events import SentTranscript
from .todos import (CorrectCheckTodo, DeliverTodo, InstallTodo, OrderDelivery, OrderProgress,
                    PullRequests, Todo, TodoOperation)
from .change_report import ChangeReport
from .messages import InfoKind, MessageKind, NoticeRole
from .native_tools import NativeExecutableTool, NativeRequestTools
from .tool_caller import ToolCaller, ThreadCaller, NoThreadCaller, ThreadListing
from .cli_result import ToolCatalogResult
from .tool_result import ToolResult
from .task_sources import (
    CurrentTaskScopeSelection, Constraint, Decision, Subtask, TaskAttachment, TaskChange,
    TaskScopeSelection, OriginalTaskChange,
)
from .turn_digests import ThreadTurns, TurnDigestRead
from .relationships import RelationshipEdit
from .work_branches import WorkBranch, WorkBranches
from .tool_output import (
    MAX_INLINE_OUTPUT_BYTES,
    materialize_oversized_output,
    serialize_tool_output,
)

if TYPE_CHECKING:
    from .comms import Comms



def _omitted_when_absent(default=None, **metadata):
    return field(default=default, metadata={"wire_omit_default": True, **metadata})


@dataclass(frozen=True)
class ModelChanged(ToolResult):
    thread: str
    model: str | None
    thinking_level: ThinkingLevelField
    service_tier: ServiceTier


@dataclass(frozen=True)
class ChannelOrderSet(ToolResult):
    order: type[ChannelSort]


@dataclass(frozen=True)
class TagList(ToolResult):
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ThreadTags(ToolResult):
    thread: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ChannelListing(ToolResult):
    channels: tuple[ChannelView, ...]
    views: tuple[SavedView, ...]
    order: type[ChannelSort]


@dataclass(frozen=True)
class ProjectSet(ToolResult):
    thread: str
    previous: str
    current: str
    changed: bool
    instruction: str


@dataclass(frozen=True)
class GoalSet(ToolResult):
    goal: Goal | None
    instruction: str


@dataclass(frozen=True)
class GoalProgress(ToolResult, GoalSnapshot):
    execution: ThreadGoalExecution = field(metadata={"wire_name": "goal_execution"})


@dataclass(frozen=True)
class GoalRead(ToolResult):
    goal: Goal | None


@dataclass(frozen=True)
class GoalHistory(ToolResult):
    history: tuple[RecordedGoalRevision, ...]


@dataclass(frozen=True)
class TodoChanged(ToolResult):
    todo: Todo
    strike_count: int


@dataclass(frozen=True)
class TodoList(ToolResult):
    todos: tuple[Todo, ...]
    progress: dict[str, OrderProgress] = field(default_factory=dict)


@dataclass(frozen=True)
class ProblemOrdered(ToolResult):
    goal: Goal
    todo: Todo


@dataclass(frozen=True)
class ProblemClosed(ToolResult):
    todo: Todo


@dataclass(frozen=True)
class WorkBranchList(ToolResult):
    branches: tuple[WorkBranch, ...]


@dataclass(frozen=True)
class CollaborationEdited(ToolResult):
    collaboration: Collaboration | None


@dataclass(frozen=True)
class VisibleCollaborator:
    peer: str
    available: bool
    sources: tuple[str, ...]
    detail: str
    goal_contacts: tuple[GoalDerivedContact, ...]


@dataclass(frozen=True)
class CollaborationList(ToolResult):
    """Goal-derived collaborators and unresolved mentions appear only when present."""
    collaborations: tuple[Collaboration, ...]
    visible_collaborators: tuple[VisibleCollaborator, ...] = _omitted_when_absent(())
    unresolved_goal_mentions: tuple[GoalMentionDiagnostic, ...] = _omitted_when_absent(())


@dataclass(frozen=True)
class MessagePublished(ToolResult):
    """The Pi adapter decodes this reply as PublishedMessageToolDetails by its agentCommsKind."""
    id: str
    message: SentTranscript
    agent_comms_kind: Literal["published_message"] = field(
        default="published_message", metadata={"wire_name": "agentCommsKind"})


@dataclass(frozen=True)
class TaskPublished(ToolResult):
    reference: MessageReference
    task: TaskAttachment


@dataclass(frozen=True)
class InboxRead(ToolResult):
    """An oversized read names its snapshot file and counts in place of the arrays."""
    messages: tuple[Message, ...]
    acknowledged: int
    unresolved_inputs: tuple[UnresolvedInput, ...]
    complete: bool | None = _omitted_when_absent()
    ack_deferred: bool | None = _omitted_when_absent(wire_name="ackDeferred")
    counts: dict[str, int] | None = _omitted_when_absent()
    result_file: str | None = _omitted_when_absent()
    instruction: str | None = _omitted_when_absent()
    standby_review: StandbyReview | StandbyReviewSummary | None = _omitted_when_absent()


@dataclass(frozen=True)
class RestartQueued(ToolResult):
    restart: QueuedRestart


@dataclass(frozen=True)
class RestartAttempts(ToolResult):
    restarts: tuple[QueuedRestart, ...]


@dataclass(frozen=True)
class RestartsCancelled(ToolResult):
    cancelled: tuple[QueuedRestart, ...]


class ContextBinding(DeclaredFamily, affix="Binding"):
    @classmethod
    @abstractmethod
    def resolve(cls, subject: str, actor: str | None) -> str: ...


class SubjectBinding(ContextBinding):
    @classmethod
    def resolve(cls, subject: str, actor: str | None) -> str:
        return subject


def tool_field(
    description: str,
    *,
    default=MISSING,
    default_factory=MISSING,
    wire_name=None,
    binding: type[ContextBinding] | None = None,
    choices=None,
):
    metadata = {"description": description, "wire_nonnull": default is not None}
    if wire_name is not None:
        metadata["wire_name"] = wire_name
    if binding is not None:
        metadata["context_binding"] = binding
    if choices is not None:
        metadata["wire_choices"] = choices
    return field(default=default, default_factory=default_factory, metadata=metadata)


@dataclass(frozen=True, kw_only=True)
class ToolRequest[Caller: ToolCaller = ToolCaller](
    ThreadCommandAvailability, NativeRequestTools, NativeExecutableTool
):
    description: ClassVar[str]
    context: ClassVar[str | None] = None
    action_label: ClassVar[str | None] = None
    action_order: ClassVar[int] = 0

    @classmethod
    def context_bindings(cls) -> dict[str, type[ContextBinding]]:
        return {
            FieldCodec.wire_key(declared): declared.metadata["context_binding"]
            for declared in fields(cls)
            if "context_binding" in declared.metadata
        }

    @classmethod
    def schema(cls) -> dict[str, object]:
        result = {
            "name": cls.declared_name,
            "label": cls.label,
            "description": cls.description,
            "parameters": FieldCodec.record_schema(cls),
        }
        if cls.context is not None:
            result.update(
                context=cls.context,
                action_label=cls.action_label or cls.label,
                action_order=cls.action_order,
                context_bindings={
                    key: binding.declared_name for key, binding in cls.context_bindings().items()
                },
            )
        return result

    @classmethod
    def invoke(
        cls, comms: Comms, arguments: Mapping[str, object], *, caller: ToolCaller
    ) -> dict[str, JsonValue]:
        """The reply every adapter receives: the encoding of the tool's declared result."""
        if cls.family_discriminator in arguments:
            raise ValueError(f"Unknown tool argument: {cls.family_discriminator}")
        try:
            request = FieldCodec.decode(cls, {**arguments, cls.family_discriminator: cls.declared_name})
        except MalformedRecord as error:
            raise MalformedRecord(f"Invalid {cls.declared_name} arguments: {error}") from error
        return FieldCodec.encode(caller.invoke(request, comms))

    @abstractmethod
    def apply(self, comms: Comms, caller: Caller) -> ToolResult:
        """Each tool declares its own result record; invoke alone encodes it."""

    def apply_without_thread(
        self: ToolRequest[ToolCaller], comms: Comms, caller: NoThreadCaller
    ) -> ToolResult:
        return self.apply(comms, caller)


class LiveOwnerTool:
    """A tool whose reply needs a launched native owner or a running restart watcher."""

    __slots__ = ()


class ReadTool:
    """A tool whose apply changes nothing on the bus; the claude.ai connector lists only these."""

    __slots__ = ()


@dataclass(frozen=True, kw_only=True)
class ThreadReadTool(ReadTool, ToolRequest):
    thread: str = tool_field("Thread to read; omit to read your own", default="")


class ThreadToolRequest(ToolRequest[ThreadCaller]):
    """Self-relative requests declare their required caller at the shared boundary."""

    def apply_without_thread(self, comms: Comms, caller: NoThreadCaller) -> NoReturn:
        return caller.refuse_thread()


def _tag_set(value: str) -> frozenset[str]:
    return frozenset(tag.strip() for tag in value.split(",") if tag.strip())


def _bounded_inbox_response(
    messages: tuple[Message, ...],
    unresolved: tuple[UnresolvedInput, ...],
    review: StandbyReview | None,
    result_file: str,
    *,
    ack: bool,
) -> InboxRead:
    """Project a saved inbox snapshot without changing any delivery or goal authority."""
    bounded = InboxRead(
        (), 0, (), complete=False, ack_deferred=ack,
        counts={"messages": len(messages), "unresolved_inputs": len(unresolved)},
        result_file=result_file,
        instruction=(
            "The messages and unresolved_inputs arrays are omitted, not empty. The file is "
            "a complete public result snapshot, not current authority. Read it selectively "
            "with read offset/limit or a JSON query; do not dump the whole file into "
            "context. No messages were acknowledged. UNKNOWN inputs are unchanged. When "
            "standby_review.messages is present, its complete eligible messages and exact "
            "reviewed_inputs are inline; other review arrays are omitted with counts. "
            "Otherwise read the review from the file. Inspect the full relevant messages "
            "before passing their reviewed_inputs to comms_goal."
        ),
    )
    if review is not None:
        bounded = replace(bounded, standby_review=review.summary())
        candidate = replace(bounded, standby_review=review.eligible_summary())
        if len(serialize_tool_output(candidate).encode("utf-8")) <= MAX_INLINE_OUTPUT_BYTES:
            return candidate
    return bounded


@dataclass(frozen=True, kw_only=True)
class CommsPinChannelTool(ToolRequest):
    label = "Pin Channel"
    description = (
        "Persist whether a channel is pinned. Pinned channels sort before unpinned "
        "channels; the selected channel sort remains the secondary order."
    )
    name: str = tool_field("Channel name")
    pinned: bool = tool_field("True to pin; false to unpin")

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return PinChannelCliCommand(name=self.name, pinned=self.pinned).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsPinThreadTool(ToolRequest):
    label = "Pin Thread in Channel"
    description = (
        "Persist whether a member is pinned within one channel. Pins do not change "
        "membership or ordering in other channels. The selected member sort remains the"
        " secondary order."
    )
    channel: str = tool_field("Channel containing the thread")
    name: str = tool_field("Member thread name")
    pinned: bool = tool_field("True to pin; false to unpin")

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelView:
        PinThreadCliCommand(channel=self.channel, name=self.name, pinned=self.pinned).apply(comms)
        return comms.views.channel_view(ChannelTarget.normalize_name(self.channel))


@dataclass(frozen=True, kw_only=True)
class CommsModelTool(NativeOwnerCommand, ToolRequest):
    label = "Change Thread Model"
    description = (
        "Change this thread's model, or another thread's model, for its future turns. "
        "Optionally change its thinking level and provider service tier too. Applies "
        "from that thread's next turn."
    )
    thread: str | None = tool_field(
        "Thread to change (defaults to the executing thread)", default=None, binding=SubjectBinding
    )
    model: str = tool_field("Provider/model identifier")
    thinking_level: str = tool_field(
        "Optional Pi thinking level", default="", choices=ThinkingLevel.names
    )
    service_tier: ServiceTier | None = tool_field(
        "Optional provider request service tier", default=None
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> ModelChanged:
        """Change this thread's model or another thread's model for future turns."""
        thread_name = caller.subject(self.thread or "")
        original = comms.registry.require(thread_name)
        if not caller.may_reconfigure(comms, thread_name):
            raise RelationViolationError(
                f"{thread_name} is running; change its settings through its own session"
            )
        model = self.model.strip()
        thinking_level = FieldCodec.decode(ThinkingLevelField, self.thinking_level.strip() or None)
        thread = comms.threads.set_thread_configuration(
            original,
            model=model or None,
            thinking_level=thinking_level,
            service_tier=self.service_tier,
        )
        return ModelChanged(thread.name, thread.model, thread.thinking_level, thread.service_tier)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelsTool(ToolRequest):
    label = "Sort Channels"
    description = (
        "Persist channel-list ordering independently of member ordering. Pinned "
        "channels stay first, with built-in channels first within each pin group."
    )
    order: type[ChannelSort] = tool_field("Channel list order")

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelOrderSet:
        return ChannelOrderSet(comms.channels.set_channel_order(self.order))


@dataclass(frozen=True, kw_only=True)
class CommsTagsTool(ToolRequest):
    label = "Manage tags"
    description = (
        "List, create, rename, or delete tags. Renaming/deleting updates thread "
        "assignments and channel filters. Uncovered tags automatically have a channel."
    )
    action: TagAction = tool_field("Operation", default=TagAction.LIST)
    name: str = tool_field("Tag name", default="")
    new_name: str = tool_field("Replacement tag for rename", default="")
    disposition: TagDisposition = tool_field("Tagged-thread operation", default_factory=KeepThreadsTagDisposition)
    confirmed: bool = tool_field("Confirm thread archiving or deletion", default=False)

    def apply(self, comms: Comms, caller: ToolCaller) -> TagList:
        tags = self.action.apply(comms.channels, self.name, self.new_name,
                                disposition=self.disposition, confirmed=self.confirmed)
        return TagList(tuple(sorted(tags)))


@dataclass(frozen=True, kw_only=True)
class CommsThreadTagsTool(ToolRequest):
    label = "Assign thread tags"
    description = (
        "Add/remove tags on a thread. Omit thread to manage your own tags. Channel "
        "membership and routing update immediately without restarting or creating an "
        "executor."
    )
    thread: str = tool_field("Thread name or alias", default="")
    add: str = tool_field("Comma-separated tags to add", default="")
    remove: str = tool_field("Comma-separated tags to remove", default="")

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadTags:
        thread = comms.channels.update_tags(
            caller.subject(self.thread),
            add=_tag_set(self.add),
            remove=_tag_set(self.remove),
        )
        return ThreadTags(thread.name, tuple(sorted(thread.tags)))


@dataclass(frozen=True, kw_only=True)
class CommsChannelsTool(ReadTool, ToolRequest):
    label = "List channels"
    description = "List named tag views and their current member threads; #any includes everyone."

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        catalog = comms.channels.catalog.read()
        return ChannelListing(comms.views.channel_views(), tuple(catalog.saved_views.values()),
                              catalog.list_order)


@dataclass(frozen=True, kw_only=True)
class CommsSetChannelMetadataTool(ToolRequest):
    label = "Set channel presentation"
    description = (
        "Set presentation-only parent and archive state. These fields never alter "
        "routing, membership, history, unread state, or delivery."
    )
    name: str = tool_field("Exact channel name")
    parent: str = tool_field("Parent channel; empty clears the parent")
    archived: bool = tool_field("Whether to hide from active presentation")

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        parent = self.parent.strip() or None
        return comms.channels.set_channel_metadata(self.name, parent=parent, archived=self.archived)


@dataclass(frozen=True, kw_only=True)
class CommsSetViewTool(ToolRequest):
    label = "Set non-routable view"
    description = (
        "Create/update a typed participant or activity projection. Views cannot receive"
        " messages and never own channel history."
    )
    name: str = tool_field("View name without #")
    kind: ViewKind = tool_field("Projection kind")
    match: type[ViewMatch] = tool_field("Tag predicate")
    tags: str = tool_field("One or more comma-separated tags")

    def apply(self, comms: Comms, caller: ToolCaller) -> SavedView:
        return comms.channels.set_saved_view(
            SavedView(self.name, self.kind, ViewPredicate(self.match, _tag_set(self.tags))))


@dataclass(frozen=True, kw_only=True)
class CommsDeleteViewTool(ToolRequest):
    label = "Delete non-routable view"
    description = "Delete a saved projection without changing tags, channels, messages, or threads."
    name: str = tool_field("Saved view name")

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        DeleteViewCliCommand(name=self.name).apply(comms)
        return CommsChannelsTool().apply(comms, caller)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelTool(ToolRequest):
    label = "Sort channel members"
    description = "Persist one channel's member ordering, shared by all attached views."
    name: str = tool_field("Channel name")
    order: type[ThreadSort] = tool_field("Member ordering")

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return comms.channels.set_channel_sort(self.name, self.order)


@dataclass(frozen=True, kw_only=True)
class CommsSetProjectTool(ThreadToolRequest):
    label = "Change Project"
    description = (
        "Change your own persistent project directory and synchronize attached clients."
        " Accepts an existing absolute path, ~ path, or path relative to your current "
        "project. Preserves this thread and its conversation. After changing, end your "
        "turn; the runtime automatically continues in the new project."
    )
    path: str = tool_field("New project directory")

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProjectSet:
        result = comms.threads.set_project(caller.name, self.path)
        return ProjectSet(
            result.thread, result.previous, result.current, result.changed,
            (
                (
                    "Project saved. End this turn now; the runtime automatically resumes this same "
                    "conversation in the new project with refreshed tools and context."
                )
                if result.changed
                else "This is already your project directory."
            ),
        )


@dataclass(frozen=True, kw_only=True)
class CommsSetGoalTool(ThreadToolRequest):
    label = "Set persistent goal"
    description = (
        "Set or replace your own persistent goal. Use this when the user asks you to "
        "start, set, or pursue a goal autonomously."
    )
    text: str = tool_field("Persistent objective to pursue")

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalSet:
        return GoalSet(
            comms.goals.update_goal(caller.name, SetGoalAction(text=self.text)),
            "Persistent goal activated. Work toward it and report progress with comms_goal.")


@dataclass(frozen=True, kw_only=True)
class CommsGoalTool(ThreadToolRequest):
    label = "Goal progress"
    description = (
        "Update goal progress; complete it or block it when user input is needed. Use "
        "standby with explicit wait_for thread names when waiting for delegated work. "
        "The goal remains active, but only a direct message from a named dependency or "
        "a user follow-up starts its next turn. Do not repeatedly announce waiting or "
        "return empty output. If existing dependency replies block standby, inspect "
        "comms_inbox with standby {goal_id, wait_for}. Read standby_review.messages and "
        "copy only standby_review.reviewed_inputs into reviewed_inputs; this records "
        "your decision to wait for a later reply without replaying uncertain inputs."
    )
    goal_id: str = tool_field("Goal identity provided in the turn context")
    status: type[GoalAction] = tool_field("Goal state", choices=GoalAction.model_choices)
    progress: str = tool_field("Progress summary; blocked requires a nonempty explicit reason")
    wait_for: list[str] | None = tool_field(
        "Explicit thread names or @names; required for standby", default=None
    )
    reviewed_inputs: list[str] | None = tool_field(
        "Exact unresolved inputId keys inspected and handled for this goal; standby only",
        default=None,
    )

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalProgress:
        name = caller.name
        command = self.status(
            progress=self.progress,
            **{
                key: value
                for key, value in (
                    ("wait_for", self.wait_for),
                    ("reviewed_inputs", self.reviewed_inputs),
                )
                if value
            },
        )
        command = replace(
            command,
            expect=GoalPrecondition(goal_id=self.goal_id, expected_state=ActiveGoal()),
        )
        comms.goals.update_goal(name, command, actor=ModelInvocable)
        return GoalProgress.from_snapshot(comms.goals.goal_snapshot(name))


@dataclass(frozen=True, kw_only=True)
class CommsResumeGoalTool(ThreadToolRequest):
    label = "Resume paused goal"
    description = (
        "Resume your own paused goal with the same ID only after an explicit user "
        "request. This does not replace the goal or resume blocked/completed goals."
    )
    goal_id: str = tool_field("Identity of the paused goal to resume")
    progress: str = tool_field("Progress summary for the resumed goal")

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalRead:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).goal_for(goal_id)
        if current is None:
            raise ValueError("This goal cannot be resumed; refresh its state.")
        current.state.require_model_resume()
        progress = self.progress
        goal = comms.goals.update_goal(
            name,
            ActiveGoalAction(
                expect=GoalPrecondition(
                    expected_goal=current,
                    goal_id=goal_id,
                ),
                progress=progress,
            ),
        )
        if goal is None or not goal.state.active or goal.progress != progress:
            raise ValueError("Goal changed during resume; refresh its state.")
        return GoalRead(goal)


@dataclass(frozen=True, kw_only=True)
class CommsEditGoalTool(ThreadToolRequest):
    label = "Edit goal text"
    description = (
        "Edit your existing goal text while keeping its identity, status, and progress."
        " Use comms_set_goal only to replace the objective with a fresh goal ID."
    )
    goal_id: str = tool_field("Identity of the existing goal to edit")
    text: str = tool_field("Revised objective text, including any @mentions")

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalRead:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).require_goal(goal_id)
        goal = comms.goals.update_goal(
            name,
            EditGoalAction(
                expect=GoalPrecondition(expected_goal=current, goal_id=goal_id), text=self.text
            ),
        )
        return GoalRead(goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalHistoryTool(ThreadReadTool):
    label = "Goal history"
    description = "Read recorded goal revisions and recorded provenance and observation gaps."
    goal_id: str = tool_field("Optional goal ID; omit for this thread's full history", default="")

    def apply(self, comms: Comms, caller: ToolCaller) -> GoalHistory:
        requested = self.goal_id.strip()
        entries = comms.goals.goal_history(caller.subject(self.thread), goal_id=requested or None)
        return GoalHistory(tuple(entry.recorded() for entry in entries))


@dataclass(frozen=True, kw_only=True)
class CommsTodoTool(ThreadToolRequest):
    label = "Change problem"
    description = "Apply an exact revision-checked Todo command; only its commander may invoke this tool."
    id: str = tool_field("Problem ID")
    operation: TodoOperation = tool_field("Typed assignment, strike, release or state command")

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged:
        todo = comms.goals.change_problem(self.id, self.operation, caller.name)
        return TodoChanged(todo, todo.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsTodoListTool(ThreadReadTool):
    label = "Read problems"
    description = "Read the commander's canonical Todo orders, deadlines, strikes and hunts."

    def apply(self, comms: Comms, caller: ToolCaller) -> TodoList:
        actor = comms.registry.require(caller.subject(self.thread)).incarnation
        listed = tuple(comms.goals.todos.for_commander(actor))
        pulls, storage = PullRequests(), comms.registry.storage_rule
        return TodoList(listed, {todo.id: OrderProgress.read(todo, storage, pulls)
                                 for todo in listed if todo.state.open and todo.holder})


@dataclass(frozen=True, kw_only=True)
class CommsOrderTool(ThreadToolRequest):
    label = "Assign problem order"
    description = "Reserve a Todo assignment and publish its Goal with a deadline; does not replay input."
    order: OrderGoalAction = tool_field("Problem, request, expected_revision, deadline, commander")
    owner: str = tool_field("Assignee thread")

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProblemOrdered:
        goal = comms.goals.update_goal(self.owner, replace(self.order, commander=caller.name))
        return ProblemOrdered(goal, comms.goals.todos.get(self.order.problem))


@dataclass(frozen=True, kw_only=True)
class CommsTodoCreateTool(CommsOrderTool):
    label = "Create problem"
    description = "Create a problem with its owner, checked order and deadline in one command."
    order: CreateProblemGoalAction = tool_field("Typed problem creation and order")


@dataclass(frozen=True, kw_only=True)
class CommsOrderDoneTool(ThreadToolRequest):
    label = "Check delivered order"
    description = "Verify the order's check on its clean delivered commit before completing its Goal."
    problem: str = tool_field("Problem ID")
    delivery: OrderDelivery = tool_field("A clean commit delivery or an operation verified by the order's check")

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalRead:
        todo = comms.goals.todos.get(self.problem)
        todo.require_commander(comms.registry.require(caller.name).incarnation)
        assignment = todo.assignment
        goal = comms.goals.update_goal(assignment.owner, DeliveredOrderGoalAction(
            expect=GoalPrecondition(goal_id=assignment.generation), problem=self.problem,
            operation=DeliverTodo(todo.revision, comms.registry.require(caller.name).incarnation, self.delivery),
            progress="The problem's own delivery check passed."))
        return GoalRead(goal)


@dataclass(frozen=True, kw_only=True)
class CommsOrderCheckTool(ThreadToolRequest):
    label = "Correct order check"
    description = ("Replace the check of an open order, recording the old check, the new one and "
                   "why; only its commander may invoke this tool, and the order keeps its deadline "
                   "and strikes.")
    problem: str = tool_field("Problem ID")
    check: OrderCheck = tool_field("The corrected check: directory, command and expected result")
    why: str = tool_field("Why the recorded check was wrong")

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged:
        todo = comms.goals.todos.get(self.problem)
        actor = comms.registry.require(caller.name).incarnation
        changed = comms.goals.change_problem(self.problem, CorrectCheckTodo(
            todo.revision, actor, todo.order.check, self.check, self.why), caller.name)
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsProblemCloseTool(ThreadToolRequest):
    label = "Close installed problem"
    description = "Record installed application evidence and close only a problem with no open order."
    problem: str = tool_field("Problem ID")
    works: str = tool_field("What worked in the installed application, 8+ words")
    artifact: str = tool_field("Existing verification artifact file")

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProblemClosed:
        todo = comms.goals.todos.get(self.problem)
        actor = comms.registry.require(caller.name).incarnation
        todo.require_commander(actor)
        closed = comms.goals.change_problem(self.problem, InstallTodo(
            todo.revision, actor, self.works, self.artifact), caller.name)
        return ProblemClosed(closed)


@dataclass(frozen=True, kw_only=True)
class CommsTurnsTool(ThreadReadTool):
    label = "Recent turns"
    description = "Read a unit's recent turns with ordered original transcript facts."
    last: int = tool_field("Number of newest turns", default=TurnDigestRead.last)

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadTurns:
        return comms.transcripts.turns(caller.subject(self.thread), last=self.last)


@dataclass(frozen=True, kw_only=True)
class CommsThreadsTool(ReadTool, ToolRequest):
    label = "Comms Threads"
    description = (
        "List agent threads with status and pending counts, plus this caller's "
        "recent sent-message delivery outcomes."
    )
    active_only: bool = tool_field("Exclude stopped threads", default=False)

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadListing:
        threads = ThreadsCliCommand(active_only=self.active_only).apply(comms)
        return caller.listing(comms, threads.threads)


@dataclass(frozen=True, kw_only=True)
class CommsWorkBranchesTool(ReadTool, ToolRequest):
    label = "Work branches"
    description = (
        "Branches with commits not on main, newest first, each with its open PR or linked "
        "open issue and the threads that have it checked out. Work on a problem must be on "
        "main or on a linked branch; unlinked_only lists the branches breaking that rule."
    )
    checkout: str = tool_field("Path of a checkout of the repository (its worktrees name owners)")
    unlinked_only: bool = tool_field("List only branches with no open PR or issue", default=True)

    def apply(self, comms: Comms, caller: ToolCaller) -> WorkBranchList:
        branches = WorkBranches(Path(self.checkout)).read(comms.registry.snapshot())
        return WorkBranchList(tuple(
            branch for branch in branches if not (self.unlinked_only and branch.linked)))


@dataclass(frozen=True, kw_only=True)
class CommsCollaborationTool(ThreadToolRequest):
    label = "Manage mutual collaboration"
    description = (
        "Create, update or end one mutual contact with another agent. Either "
        "participant can add, change its shared note, or remove it from both "
        "collaboration lists. This persistent metadata does not message, wake or fork "
        "either agent."
    )
    action: type[RelationshipEdit] = tool_field("Relationship change")
    peer: str = tool_field("Collaborating agent thread name or alias")
    note: str = tool_field("Short description of ongoing work", default="")

    def apply(self, comms: Comms, caller: ThreadCaller) -> CollaborationEdited:
        return CollaborationEdited(
            comms.relationships.edit(caller.name, self.action, self.peer, self.note))


@dataclass(frozen=True, kw_only=True)
class CommsCollaborationsTool(ThreadReadTool):
    label = "List collaborations"
    description = (
        "Read explicit contacts and goal-derived awareness with provenance, without "
        "changing delivery, work acceptance or owners."
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> CollaborationList:
        contacts = comms.relationships.contacts(caller.subject(self.thread))
        visible = tuple(
            VisibleCollaborator(entry.target, entry.available, entry.sources, entry.detail,
                                entry.goal_contacts)
            for entry in contacts.visible
        ) if any(entry.goal_contacts for entry in contacts.visible) else ()
        return CollaborationList(contacts.explicit, visible, contacts.diagnostics)


@dataclass(frozen=True, kw_only=True)
class CommsRenameSelfTool(ThreadToolRequest):
    label = "Rename Comms Thread"
    description = "Rename your own persistent thread identity; old names remain routing aliases."
    new_name: str = tool_field("Your new thread name")

    def apply(self, comms: Comms, caller: ThreadCaller) -> RenameThreadResult:
        return comms.threads.rename_self(self.new_name, caller=caller)


@dataclass(frozen=True, kw_only=True)
class CommsSendTool(ToolRequest):
    label = "Comms Send"
    description = (
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text."
    )
    sender: str = tool_field("Sender thread name", wire_name="from")
    target: str = tool_field("Target thread name or channel", wire_name="to")
    body: str = tool_field("Message text")
    type: Type[MessageKind] = tool_field(
        "Message kind", default=InfoKind, choices=MessageKind.sendable_names
    )
    report: ChangeReport | None = tool_field(
        "Change report fields; required for the change-report kind", default=None
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> MessagePublished:
        message = comms.messaging.send_message(
            self.sender, self.target, self.body, self.type, report=self.report
        )
        return MessagePublished(message.message_id, SentTranscript.from_message(message))


@dataclass(frozen=True, kw_only=True)
class CommsAuthoredTaskTool(ThreadToolRequest):
    """One original authorized publication path for authored task declarations."""
    target: str = tool_field("Thread or channel receiving the original declaration", wire_name="to")
    scope: TaskScopeSelection = tool_field(
        "Current project/goal or explicit scope", default=CurrentTaskScopeSelection())
    change: TaskChange = tool_field(
        "Original declaration or correction naming its original reference",
        default=OriginalTaskChange())

    @abstractmethod
    def declaration(self, owner): ...

    @abstractmethod
    def original_body(self, declaration): ...

    def apply(self, comms: Comms, caller: ThreadCaller) -> TaskPublished:
        owner = comms.registry.require(caller.name)
        declaration = self.declaration(owner)
        message = comms.messaging.send_message(
            owner.name, self.target, self.original_body(declaration),
            role=NoticeRole(), task=declaration)
        return TaskPublished(message.reference, message.task)


@dataclass(frozen=True, kw_only=True)
class CommsDecisionTool(CommsAuthoredTaskTool):
    label = "Record Decision"
    description = (
        "Record a chosen alternative and valid rejected alternatives on its original "
        "wire message. Author and source turn come from your authorized execution. "
        "Defaults to current project/goal revision or current turn; corrections name "
        "the original reference and grant no execution.")
    chosen: str = tool_field("Chosen alternative, preserving exact wording")
    rejected: tuple[str, ...] = tool_field("Nonempty unique valid rejected alternatives")

    def declaration(self, owner):
        return Decision.from_claim(owner, self.scope, self.change,
                                       chosen=self.chosen, rejected=self.rejected)

    def original_body(self, declaration):
        return declaration.text


@dataclass(frozen=True, kw_only=True)
class CommsConstraintTool(CommsAuthoredTaskTool):
    label = "Record Constraint"
    description = (
        "Declare an exact authored restriction on its original wire message; wording "
        "is retained verbatim. This records your authorized authorship, not inferred "
        "human authority. Defaults to current project/goal revision or current turn. "
        "Corrections name the original constraint reference; no execution or replay "
        "is authorized.")
    text: str = tool_field("Exact authored restriction, never a paraphrase of another source")

    def declaration(self, owner):
        return Constraint.from_claim(owner, self.scope, self.change)

    def original_body(self, declaration):
        if not self.text.strip():
            raise ValueError("An authored constraint requires exact nonempty wording")
        return self.text


@dataclass(frozen=True, kw_only=True)
class CommsSubtaskTool(CommsAuthoredTaskTool):
    label = "Record Subtask"
    description = (
        "Explicitly record whether a described subtask is completed or unfinished. "
        "This is not assistant-turn termination or goal completion. Original author, "
        "turn, scope and correction reference use the existing authorized task owner. "
        "A completed observation may permit optional compaction when enabled.")
    text: str = tool_field("Exact description of the observed subtask")
    completed: bool = tool_field("Your explicit observation: completed or unfinished")

    def declaration(self, owner):
        if not self.text.strip():
            raise ValueError("A subtask observation requires its exact description")
        return Subtask.from_claim(owner, self.scope, self.change, completed=self.completed)

    def original_body(self, declaration):
        return self.text


@dataclass(frozen=True)
class StandbyRequest:
    goal_id: str = tool_field("Current goal for scoped standby review")
    wait_for: tuple[str, ...] = tool_field("Declared dependencies for standby review")


@dataclass(frozen=True, kw_only=True)
class CommsInboxTool(ToolRequest):
    label = "Comms Inbox"
    description = (
        "Fetch undelivered messages and unresolved native input attempts for a thread. "
        "Optional target limits the conversation. ACK marks exactly the returned "
        "message references; unresolved_inputs remain UNKNOWN. Oversized results return "
        "complete=false, counts, and a complete result_file; ACK is deferred. Read that "
        "file selectively, never dump it into context. For standby, supply standby "
        "(goal_id and wait_for) to get standby_review: exact eligible keys and messages, "
        "already reviewed entries, and excluded owner/other-dependency inputs."
    )
    thread: str = tool_field("Thread name")
    target: str | None = tool_field("Optional channel or peer conversation", default=None)
    ack: bool = tool_field("Mark exactly the returned messages delivered", default=True)
    standby: StandbyRequest | None = tool_field("Goal and dependencies to review", default=None)

    def apply(self, comms: Comms, caller: ToolCaller) -> InboxRead:
        thread = self.thread
        target = self.target
        if target is not None:
            target = target.strip()
            if not target:
                raise ValueError("An inbox target cannot be empty.")
        standby = self.standby
        review = (
            comms.goals.goal_input_review(thread, standby.goal_id, standby.wait_for)
            if standby is not None
            else None
        )
        messages = tuple(comms.bus.inbox(thread, target))
        unresolved = tuple(comms.goals.unresolved_inputs(thread))
        response = InboxRead(messages, 0, unresolved, standby_review=review)
        result_file = materialize_oversized_output(
            comms.root, response, inline_limit=MAX_INLINE_OUTPUT_BYTES - 64
        )
        if result_file is not None:
            return _bounded_inbox_response(
                messages, unresolved, review, str(result_file), ack=self.ack
            )
        references = tuple(message.reference for message in messages)
        return replace(response, acknowledged=(
            comms.messaging.acknowledge(thread, references) if self.ack else 0))


@dataclass(frozen=True, kw_only=True)
class CommsForkTool(LiveOwnerTool, NativeOwnerCommand, ToolRequest):
    label = "Comms Fork"
    description = "Fork a child agent thread from a registered parent's persistent session."
    context = "thread"
    action_label = "Fork from this thread"
    action_order = 10
    name: str = tool_field("Child thread name")
    parent: str = tool_field("Parent thread name", binding=SubjectBinding)
    task: str = tool_field("Optional task for the child", default="")
    tags: str | None = tool_field(
        "Comma-separated tags; omitted inherits parent tags", default=None
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadCreatedResult:
        parent = comms.registry.require(self.parent)
        return ForkCliCommand(name=self.name, parent=parent.name, task=self.task,
                              project=parent.worktree, model=parent.model,
                              thinking_level=parent.thinking_level,
                              tags=_tag_set(self.tags) if self.tags is not None else None).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsStopTool(NativeOwnerCommand, OwnerLifecycleControl, ToolRequest):
    label = "Stop Comms Thread"
    description = "Stop a thread after verifying that its process owns the registered identity."
    context = "thread"
    action_label = "Stop process"
    action_order = 20
    name: str = tool_field("Thread name", binding=SubjectBinding)

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadStoppedResult:
        return StopCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsStartTool(LiveOwnerTool, NativeOwnerCommand, StartOwnerControl, ToolRequest):
    label = "Start Comms Thread"
    description = (
        "Start a stopped agent thread with its saved conversation and configuration. "
        "Reuses an already-running owner without interrupting it. The returned PID is "
        "reserved; startup may still be in progress. Does not send a new task."
    )
    context = "thread"
    action_label = "Start thread"
    action_order = 21
    name: str = tool_field("Thread to start", binding=SubjectBinding)

    def apply(self, comms: Comms, caller: ToolCaller) -> OwnerStartResult:
        return StartCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsQueueRestartTool(LiveOwnerTool, NativeOwnerCommand, OwnerLifecycleControl, ToolRequest):
    label = "Queue Idle Owner Restart"
    description = (
        "Queue an exact live owner incarnation for restart when idle. Never interrupt an active "
        "turn. A changed owner or uncertain attempt is never retried automatically. "
        "Restarts in the calling process's runtime; does not deploy another runtime."
    )
    context = "thread"
    action_label = "Queue idle restart"
    action_order = 22
    name: str = tool_field("Live agent thread", binding=SubjectBinding)

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartQueued:
        return RestartQueued(
            enqueue_restart(comms, self.name, launch_runtime=LaunchRuntime.running()))


@dataclass(frozen=True, kw_only=True)
class CommsRestartQueueTool(ReadTool, ToolRequest):
    label = "Restart Queue Status"
    description = "Inspect queued, restarted, stale, or uncertain attempts for a thread."
    name: str = tool_field("Agent thread")

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartAttempts:
        return RestartAttempts(tuple(restart_status(comms, self.name)))


@dataclass(frozen=True, kw_only=True)
class CommsCancelRestartTool(ToolRequest):
    label = "Cancel Queued Restart"
    description = "Cancel pending restarts for a thread; cannot undo an attempted restart."
    name: str = tool_field("Agent thread")

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartsCancelled:
        return RestartsCancelled(tuple(cancel_restart(comms, self.name)))


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(ArchiveOwnerControl, ToolRequest):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    context = "thread"
    action_label = "Archive stopped thread"
    action_order = 30
    name: str = tool_field("Stopped thread name", binding=SubjectBinding)

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadArchivedResult:
        return ArchiveCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class ToolsCliCommand(CliCommand):
    help = "Emit the shared adapter tool catalog"
    name: str | None = option("--name", default=None, help="Calling thread")

    def apply(self, ctx: Comms) -> ToolCatalogResult:
        caller = ToolCaller.from_actor(self.name)
        return caller.catalog(ctx, tuple(NativeRequestTools.catalog()))


@dataclass(frozen=True, kw_only=True)
class InvokeCliCommand(CliCommand, declared_name="tool"):
    help = "Invoke one declared adapter tool, as --from THREAD when given"
    caller: str | None = option(
        "--from", default=None, help="Calling thread; omit for tools that need no thread"
    )
    tool: str = option("--tool")
    arguments: dict[str, object] = option(
        "--arguments", help="Tool arguments as a JSON object", default_factory=dict,
        parser_default="{}", normalize=_json_object)

    def apply(self, ctx: Comms) -> dict[str, JsonValue]:
        return invoke_tool(ctx, self.tool, self.arguments,
                           caller=ToolCaller.from_actor(self.caller))

    def run(self, ctx: Comms, output: TextIO) -> int:
        """The tool protocol's own reply, the one every adapter receives, not a CLI result."""
        json.dump(self.apply(ctx), output, indent=2)
        output.write("\n")
        return 0


def context_tool_catalog(context: str) -> list[dict[str, object]]:
    tools = sorted(
        (tool for tool in ToolRequest.members_with(ToolRequest) if tool.context == context),
        key=lambda tool: tool.action_order,
    )
    return [tool.schema() for tool in tools]


def invoke_tool(
    comms: Comms, name: str, arguments: Mapping[str, object], *, caller: ToolCaller
) -> dict[str, JsonValue]:
    return ToolRequest.decode(name).invoke(comms, arguments, caller=caller)


def invoke_context_tool(
    comms: Comms,
    name: str,
    *,
    subject: str,
    actor: str | None,
    arguments: Mapping[str, object] | None = None,
) -> dict[str, JsonValue]:
    tool = ToolRequest.decode(name)
    if tool.context is None:
        raise ValueError(f"Tool {name!r} is not a context action.")
    bound = dict(arguments or {})
    bound.update(
        (key, binding.resolve(subject, actor)) for key, binding in tool.context_bindings().items()
    )
    return tool.invoke(comms, bound, caller=ToolCaller.from_actor(actor))
