"""Named external tool requests; declarations own schema, context and behavior."""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, NoReturn, TextIO, Type, Annotated

from pydantic import JsonValue


from . import cli as comms_cli
from .change_report import NoChangeReport, ReportEvidence
from .channel_management import KeepThreadsTagDisposition, TagAction, TagDisposition
from .channel_targets import ChannelTarget, CommandTarget
from .channels import ANY_CHANNEL, Channel, SavedView, ViewKind, ViewMatch, ViewPredicate
from .cli_commands import (
    ArchiveCliCommand,
    CliCommand,
    DeleteViewCliCommand,
    ForkCliCommand,
    HistoryCliCommand,
    PinChannelCliCommand,
    PinThreadCliCommand,
    StartCliCommand,
    StopCliCommand,
    ThreadArchivedResult,
    ThreadCreatedResult,
    ThreadsCliCommand,
    ThreadStoppedResult,
)
from .cli_result import ToolCatalogResult
from .commander.capacity import Load
from .coordination_tables.refusals import SendRefusal
from .declared_family import DeclaredFamily
from .display_order import ChannelSort, ThreadSort
from .errors import RelationViolationError, ToolRefusedError
from .field_codec import (
    FieldCodec,
    MalformedRecord,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    projected,
    Argument,
    ContextSource,
    DefaultNonNull,
    Description,
    FieldDirective,
    WireChoices,
    WireName,
    WireOmitDefault,
)
from .launch_runtime import LaunchRuntime
from .goal_actions import (
    ActiveGoalAction,
    CreateProblemGoalAction,
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    ModelInvocable,
    OrderGoalAction,
    SetGoalAction,
    TransitionGoalAction,
)
from .goal_history import RecordedGoalRevision
from .goal_presentation import GoalSnapshot, ThreadGoalExecution
from .goal_waits import StandbyReview
from .goals import Goal, GoalDeclaration, OrderCheck
from .standing_rules import StandingRule
from .plan_phases import OrderPhase
from .message_reference import MessageReference
from .message_page import PageBounds
from .history_result import HistoryPageResult
from .input_attempt import UnresolvedInput
from .message_page import PageBounds
from .message_reference import MessageReference
from .messages import InfoKind, Message, MessageKind, NoticeRole, SendableMessageKind
from .native_tools import ArgumentTool, NativeRequestTools, NativeToolDeclaration
from .page_traversal import NewestCursor, PageCursor
from .owner_lifecycle import OwnerStartResult, OwnerRestartSelection
from .presentation import ChannelView
from .read_basis import NamedViewer
from .relationships import Collaboration, GoalDerivedContact, GoalMentionDiagnostic
from .restart_queue import (
    QueuedRestart,
    cancel as cancel_restart,
    enqueue as enqueue_restart,
    status as restart_status,
)
from .thread_management import InheritedTags, RenameThreadResult, TagChoice
from .threads import NextTurnConfiguration
from .transcript_events import SentTranscript
from .todos import (
    CorrectCheckTodo,
    ServeTodo,
    DeliverTodo,
    InstallTodo,
    OrderDelivery,
    OrderProgress,
    ToolCallVerdict,
    PlainText,
    PullRequests,
    StrikeReason,
    Todo,
    TodoOperation,
    VerdictAudience,
)
from .tool_caller import ToolCaller, ThreadCaller, NoThreadCaller, ThreadListing
from .thread_identity import ThreadIncarnation
from .tool_result import ToolResult
from .pi_payloads import PublishedGoalToolDetails
from .send_result import CorrectionResult, SendResult
from .task_sources import (
    CurrentTaskScopeSelection,
    Constraint,
    Decision,
    Subtask,
    TaskAttachment,
    TaskChange,
    TaskScopeSelection,
    OriginalTaskChange,
)
from .order_story import FleetStory, OrderStory
from .order_work import OrderWork
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .plan_phases import OrderPhase
from .progress_claims import ProgressClaim
from .relationships import RelationshipEdit
from .thread_execution import (
    ArchiveOwnerControl,
    NativeOwnerCommand,
    OwnerLifecycleControl,
    StartOwnerControl,
    ThreadCommandAvailability,
)
from .tool_output import MAX_INLINE_OUTPUT_BYTES
from .turn_digests import (
    LastTurns,
    OrderEditsRead,
    ThreadTurns,
    TurnsSince,
    UnjudgedEdits,
)
from .work_branches import WorkBranch, WorkBranches

if TYPE_CHECKING:
    from .comms import Comms


class ToolArgumentsRefusedError(MalformedRecord, ToolRefusedError):
    """Bad external tool arguments, not malformed stored state or a broken implementation."""


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


class OwnGoalPublication(ToolResult):
    """A self-relative goal writer returns the exact declaration it published."""

    __slots__ = ()

    @projected(view="wire", name="agentCommsKind")
    def publication_kind(self) -> object:
        return FieldCodec.encode(PublishedGoalToolDetails)


@dataclass(frozen=True)
class GoalSet(OwnGoalPublication):
    goal: GoalDeclaration
    instruction: str


@dataclass(frozen=True)
class GoalProgress(OwnGoalPublication, GoalSnapshot):
    execution: Annotated[ThreadGoalExecution, WireName("goal_execution")]


@dataclass(frozen=True)
class GoalUpdated(OwnGoalPublication):
    goal: GoalDeclaration


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
class StandingRulesRead(ToolResult):
    broken: tuple[str, ...]


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
    visible_collaborators: Annotated[
        tuple[VisibleCollaborator, ...], WireOmitDefault()
    ] = ()
    unresolved_goal_mentions: Annotated[
        tuple[GoalMentionDiagnostic, ...], WireOmitDefault()
    ] = ()


@dataclass(frozen=True)
class MessagePublished(ToolResult):
    """The Pi adapter decodes this reply as PublishedMessageToolDetails by its agentCommsKind."""

    id: str
    message: SentTranscript
    correction: CorrectionResult
    agent_comms_kind: Annotated[
        Literal["published_message"], WireName("agentCommsKind")
    ] = "published_message"

    @classmethod
    def from_send(cls, result: SendResult) -> MessagePublished:
        return cls(result.message.message_id, SentTranscript.from_message(result.message), result.correction)


@dataclass(frozen=True)
class TaskPublished(ToolResult):
    reference: MessageReference
    task: TaskAttachment
    correction: CorrectionResult


@dataclass(frozen=True)
class InboxPageRead(ToolResult):
    """One unread page; only its returned references can be acknowledged."""

    messages: tuple[Message, ...]
    acknowledged: int
    remaining: int
    unresolved_inputs: tuple[UnresolvedInput, ...]
    standby_review: Annotated[StandbyReview | None, WireOmitDefault()] = (
        None
    )


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
    wire_name=None,
    binding: type[ContextBinding] | None = None,
    choices=None,
) -> tuple[FieldDirective, ...]:
    directives: list[FieldDirective] = [
        Description(description),
        DefaultNonNull(),
    ]
    if wire_name is not None:
        directives.append(WireName(wire_name))
    if binding is not None:
        directives.append(ContextSource(binding))
    if choices is not None:
        directives.append(WireChoices(choices))
    return tuple(directives)


@dataclass(frozen=True, kw_only=True)
class ToolRequest[Caller: ToolCaller = ToolCaller](
    ArgumentTool, ThreadCommandAvailability, NativeRequestTools, NativeToolDeclaration
):
    description: ClassVar[str]
    bus_effect: ClassVar[str] = (
        "Changes the bus. Tristan has authorized claude-ai to call this without asking him."
    )
    context: ClassVar[str | None] = None
    action_label: ClassVar[str | None] = None
    action_order: ClassVar[int] = 0

    @classmethod
    def context_bindings(cls) -> dict[str, type[ContextBinding]]:
        return {
            plan.key: binding.value
            for plan in FieldCodec.field_plans(cls)
            for binding in ContextSource.single(plan.annotation)
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

    def cli_command(self, caller: ToolCaller) -> InvokeCliCommand:
        """Project this declaration into Core's existing external tool command."""
        return InvokeCliCommand(caller=caller.acting_name(''),
                                tool=self.declared_name, arguments=self.arguments)

    @classmethod
    def invoke(
        cls, comms: Comms, arguments: Mapping[str, object], *, caller: ToolCaller
    ) -> dict[str, JsonValue]:
        """The reply every adapter receives: the encoding of the tool's declared result."""
        if cls.family_discriminator in arguments:
            raise ToolArgumentsRefusedError(f"Unknown tool argument: {cls.family_discriminator}")
        try:
            request = FieldCodec.decode(cls, {**arguments, cls.family_discriminator: cls.declared_name})
        except MalformedRecord as error:
            raise ToolArgumentsRefusedError(
                f"Invalid {cls.declared_name} arguments: {error}"
            ) from error
        return FieldCodec.encode(caller.invoke(request, comms).to_wire())

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
    """A tool whose apply changes nothing on the bus."""

    __slots__ = ()
    bus_effect: ClassVar[str] = "Reads only; changes nothing on the bus."


@dataclass(frozen=True, kw_only=True)
class ThreadReadTool(ReadTool, ToolRequest):
    thread: Annotated[str, *tool_field("Thread to read; omit to read your own")] = (
        ""
    )


class ThreadToolRequest(ToolRequest[ThreadCaller]):
    """Self-relative requests declare their required caller at the shared boundary."""

    def apply_without_thread(self, comms: Comms, caller: NoThreadCaller) -> NoReturn:
        return caller.refuse_thread()


def _tag_set(value: str) -> frozenset[str]:
    return frozenset(tag.strip() for tag in value.split(",") if tag.strip())


@dataclass(frozen=True, kw_only=True)
class CommsPinChannelTool(ToolRequest):
    label = "Pin Channel"
    description = (
        "Persist whether a channel is pinned. Pinned channels sort before unpinned "
        "channels; the selected channel sort remains the secondary order."
    )
    name: Annotated[str, *tool_field("Channel name")]
    pinned: Annotated[bool, *tool_field("True to pin; false to unpin")]

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
    channel: Annotated[str, *tool_field("Channel containing the thread")]
    name: Annotated[str, *tool_field("Member thread name")]
    pinned: Annotated[bool, *tool_field("True to pin; false to unpin")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelView:
        PinThreadCliCommand(channel=self.channel, name=self.name, pinned=self.pinned).apply(comms)
        return comms.views.channel_view(ChannelTarget.normalize_name(self.channel))


class ToolSubject(SubclassUnion, affix="Subject"):
    """The thread a tool call acts on: one it names, or the calling thread."""

    @abstractmethod
    def name(self, caller: ToolCaller) -> str: ...


@dataclass(frozen=True, slots=True)
class NamedSubject(SingleFieldValue, ToolSubject):
    thread: str

    def name(self, caller: ToolCaller) -> str:
        return self.thread


@dataclass(frozen=True, slots=True)
class CallerSubject(NullMember, ToolSubject):
    def name(self, caller: ToolCaller) -> str:
        return caller.own_thread()


@dataclass(frozen=True, kw_only=True)
class CommsModelTool(NextTurnConfiguration, NativeOwnerCommand, ToolRequest):
    label = "Change Thread Model"
    description = (
        "Change this thread's model, or another thread's model, for its future turns. "
        "Optionally change its thinking level and provider service tier too. Applies "
        "from that thread's next turn."
    )
    thread: Annotated[
        ToolSubject,
        *tool_field("Thread to change (defaults to the executing thread)", binding=SubjectBinding),
    ] = CallerSubject()

    def apply(self, comms: Comms, caller: ToolCaller) -> ModelChanged:
        """Change this thread's model or another thread's model for future turns."""
        thread_name = self.thread.name(caller)
        original = comms.registry.require(thread_name)
        if not caller.may_reconfigure(comms, thread_name):
            raise RelationViolationError(
                f"{thread_name} is running; change its settings through its own session"
            )
        thread = comms.threads.set_thread_configuration(original, self)
        return ModelChanged(thread.name, thread.model, thread.thinking_level, thread.service_tier)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelsTool(ToolRequest):
    label = "Sort Channels"
    description = (
        "Persist channel-list ordering independently of member ordering. Pinned "
        "channels stay first, with built-in channels first within each pin group."
    )
    order: Annotated[type[ChannelSort], *tool_field("Channel list order")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelOrderSet:
        return ChannelOrderSet(comms.channels.set_channel_order(self.order))


@dataclass(frozen=True, kw_only=True)
class CommsTagsTool(ToolRequest):
    label = "Manage tags"
    description = (
        "List, create, rename, or delete tags. Renaming/deleting updates thread "
        "assignments and channel filters. Uncovered tags automatically have a channel."
    )
    action: Annotated[TagAction, *tool_field("Operation")] = TagAction.LIST
    name: Annotated[str, *tool_field("Tag name")] = ""
    new_name: Annotated[str, *tool_field("Replacement tag for rename")] = ""
    disposition: Annotated[TagDisposition, *tool_field("Tagged-thread operation")] = (
        dataclasses.field(default_factory=KeepThreadsTagDisposition)
    )
    confirmed: Annotated[bool, *tool_field("Confirm thread archiving or deletion")] = (
        False
    )

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
    thread: Annotated[str, *tool_field("Thread name or alias")] = ""
    add: Annotated[str, *tool_field("Comma-separated tags to add")] = ""
    remove: Annotated[str, *tool_field("Comma-separated tags to remove")] = (
        ""
    )

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
    name: Annotated[str, *tool_field("Exact channel name")]
    parent: Annotated[str, *tool_field("Parent channel; empty clears the parent")]
    archived: Annotated[bool, *tool_field("Whether to hide from active presentation")]

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
    name: Annotated[str, *tool_field("View name without #")]
    kind: Annotated[ViewKind, *tool_field("Projection kind")]
    match: Annotated[type[ViewMatch], *tool_field("Tag predicate")]
    tags: Annotated[str, *tool_field("One or more comma-separated tags")]

    def apply(self, comms: Comms, caller: ToolCaller) -> SavedView:
        return comms.channels.set_saved_view(
            SavedView(self.name, self.kind, ViewPredicate(self.match, _tag_set(self.tags))))


@dataclass(frozen=True, kw_only=True)
class CommsDeleteViewTool(ToolRequest):
    label = "Delete non-routable view"
    description = "Delete a saved projection without changing tags, channels, messages, or threads."
    name: Annotated[str, *tool_field("Saved view name")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        DeleteViewCliCommand(name=self.name).apply(comms)
        return CommsChannelsTool().apply(comms, caller)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelTool(ToolRequest):
    label = "Sort channel members"
    description = "Persist one channel's member ordering, shared by all attached views."
    name: Annotated[str, *tool_field("Channel name")]
    order: Annotated[type[ThreadSort], *tool_field("Member ordering")]

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
    path: Annotated[str, *tool_field("New project directory")]

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
    text: Annotated[str, *tool_field("Persistent objective to pursue")]

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
    goal_id: Annotated[str, *tool_field("Goal identity provided in the turn context")]
    status: Annotated[
        type[TransitionGoalAction], *tool_field("Goal state", choices=GoalAction.model_choices)
    ]
    progress: Annotated[
        str, *tool_field("Progress summary; blocked requires a nonempty explicit reason")
    ]
    wait_for: Annotated[
        tuple[str, ...] | None, *tool_field("Explicit thread names or @names; required for standby")
    ] = None
    reviewed_inputs: Annotated[
        tuple[str, ...] | None,
        *tool_field(
            "Exact unresolved inputId keys inspected and handled for this goal; standby only"
        ),
    ] = None
    claims: Annotated[
        tuple[ProgressClaim, ...],
        *tool_field(
            "Merged, installed or working claims, each with the artifact Core verifies; "
            "a completed report needs at least one"
        ),
    ] = ()

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalProgress:
        name = caller.name
        self.status.require_claims(self.claims)
        ProgressClaim.require_all(self.claims, comms, Path(comms.registry.require(name).worktree))
        command = replace(
            self.status(progress=self.progress),
            expect=self.status.model_precondition(self.goal_id),
            **{key: value for key, value in (
                ("wait_for", self.wait_for), ("reviewed_inputs", self.reviewed_inputs),
            ) if value},
        )
        return GoalProgress.from_snapshot(
            comms.goals.update_goal_snapshot(name, command, actor=ModelInvocable))


@dataclass(frozen=True, kw_only=True)
class CommsResumeGoalTool(ThreadToolRequest):
    label = "Resume paused goal"
    description = (
        "Resume your own paused goal with the same ID only after an explicit user "
        "request. This does not replace the goal or resume blocked/completed goals."
    )
    goal_id: Annotated[str, *tool_field("Identity of the paused goal to resume")]
    progress: Annotated[str, *tool_field("Progress summary for the resumed goal")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalUpdated:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).goal.require().require_id(goal_id)
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
        goal = goal.require()
        if not goal.state.active or goal.progress != progress:
            raise ToolRefusedError("Goal changed during resume; refresh its state.")
        return GoalUpdated(goal)


@dataclass(frozen=True, kw_only=True)
class CommsEditGoalTool(ThreadToolRequest):
    label = "Edit goal text"
    description = (
        "Edit your existing goal text while keeping its identity, status, and progress."
        " Use comms_set_goal only to replace the objective with a fresh goal ID."
    )
    goal_id: Annotated[str, *tool_field("Identity of the existing goal to edit")]
    text: Annotated[str, *tool_field("Revised objective text, including any @mentions")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalUpdated:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).goal.require().require_id(goal_id)
        goal = comms.goals.update_goal(
            name,
            EditGoalAction(
                expect=GoalPrecondition(expected_goal=current, goal_id=goal_id), text=self.text
            ),
        )
        return GoalUpdated(goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalHistoryTool(ThreadReadTool):
    label = "Goal history"
    description = "Read recorded goal revisions and recorded provenance and observation gaps."
    goal_id: Annotated[
        str, *tool_field("Optional goal ID; omit for this thread's full history")
    ] = ""

    def apply(self, comms: Comms, caller: ToolCaller) -> GoalHistory:
        requested = self.goal_id.strip()
        entries = comms.goals.goal_history(caller.subject(self.thread), goal_id=requested or None)
        return GoalHistory(tuple(entry.recorded() for entry in entries))


@dataclass(frozen=True, kw_only=True)
class CommsTodoTool(ThreadToolRequest):
    label = "Change problem"
    description = (
        "Apply an exact revision-checked Todo command; only its commander may invoke this tool."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoChanged | ProblemClosed:
        todo = comms.goals.todos.get(self.problem)
        actor = comms.registry.require(caller.name).incarnation
        return self.result(comms.goals.change_problem(
            self.problem, self.operation_for(todo, actor, comms), caller.name))

    @abstractmethod
    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        """Build the change, verifying any attached application claims before writing."""

    def result(self, changed: Todo) -> TodoChanged | ProblemClosed:
        return TodoChanged(changed, changed.strike_count)


@dataclass(frozen=True, kw_only=True)
class CommsChangeProblemTool(CommsTodoTool, declared_name="comms_todo"):
    operation: Annotated[
        TodoOperation, *tool_field("Typed assignment, strike, release or state command")
    ]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return self.operation


@dataclass(frozen=True, kw_only=True)
class CommsVerdictTool(CommsChangeProblemTool):
    label = "Judge order tool call"
    description = (
        "Record the commander's verdict on one tool call the holder made for its "
        "order: an accepted verdict quotes 4+ words of the order; a send-back strikes "
        "the holder and revises its Goal in the same write."
    )
    operation: Annotated[
        ToolCallVerdict, *tool_field("Accepted or send-back verdict on the judged tool call ID")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsUnjudgedTool(ThreadReadTool):
    label = "Unjudged edits"
    description = "Read original edit calls since each open order, excluding that order's recorded verdicts."

    def apply(self, comms: Comms, caller: ToolCaller) -> UnjudgedEdits:
        return UnjudgedEdits(tuple(edit for read in OrderEditsRead.for_commander(
            comms, caller.subject(self.thread)) for edit in read.read()))


@dataclass(frozen=True, kw_only=True)
class CommsCommanderLoadTool(ThreadReadTool):
    label = "Measured commander load"
    description = "Read canonical order/edit debt and receiving child commanders; never self-reported."

    def apply(self, comms: Comms, caller: ToolCaller) -> Load:
        return Load.of(comms, caller.subject(self.thread))


@dataclass(frozen=True, kw_only=True)
class CommsFireTool(ThreadToolRequest):
    label = "Fire unit"
    description = (
        "Release every open problem the unit holds in one operation, recording one "
        "plain reason on each; only their commander may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Unit thread to fire")]
    reason: Annotated[StrikeReason, *tool_field("What the unit did, plainly")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> TodoList:
        return TodoList(comms.goals.fire(self.unit, self.reason, caller.name))


@dataclass(frozen=True, kw_only=True)
class CommsCorrectWordingTool(ThreadToolRequest):
    label = "Correct unit wording"
    description = (
        "Answer a unit's jargon with a correction naming its open orders; the unit's "
        "strikes stay as they are. Only its commander may invoke this tool."
    )
    unit: Annotated[str, *tool_field("Unit thread whose message used jargon")]
    message_id: Annotated[str, *tool_field("The unit's message")]
    text: Annotated[str, *tool_field("The unit's message text")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> MessagePublished:
        actor = comms.registry.require(caller.name).incarnation
        orders = [todo.id for todo in comms.goals.todos.for_commander(actor)
                  if todo.holder == self.unit]
        if not orders:
            raise ToolRefusedError(f"{self.unit} holds no open order from {caller.name}.")
        if not (words := PlainText.found(self.text)):
            raise ToolRefusedError(
                f"Message {self.message_id} is plain; there is nothing to correct."
            )
        result = comms.messaging.send(
            caller.name, self.unit,
            f"Correction, no strike, on {', '.join(orders)}: message {self.message_id} used "
            f"{', '.join(words)}. Say what you did and what broke in plain words from now on.")
        return MessagePublished.from_send(result)


@dataclass(frozen=True, kw_only=True)
class CommsTodoListTool(ThreadReadTool):
    label = "Read problems"
    description = "Read the commander's canonical Todo orders, deadlines, strikes and hunts."

    def apply(self, comms: Comms, caller: ToolCaller) -> TodoList:
        actor = comms.registry.require(caller.subject(self.thread)).incarnation
        listed = tuple(comms.goals.todos.for_commander(actor))
        pulls, storage = PullRequests(listed), comms.registry.storage_rule
        return TodoList(listed, {todo.id: OrderProgress.read(todo, storage, pulls)
                                 for todo in listed if todo.holder})


@dataclass(frozen=True, kw_only=True)
class CommsStandingRulesTool(ThreadToolRequest):
    label = "Read broken standing rules"
    description = (
        "Every standing rule the commander breaks now, one line each, derived on read; "
        "Core first does what each broken rule does itself."
    )
    after: Annotated[
        float, *tool_field("Only rules broken since this time; 0 lists every one")
    ] = 0.0

    def apply(self, comms: Comms, caller: ThreadCaller) -> StandingRulesRead:
        broken = StandingRule.broken(comms, caller.name)
        for rule in broken:
            rule.enforce(comms, caller.name)
        return StandingRulesRead(
            tuple(rule.line() for rule in broken if rule.broke_after(self.after)))


@dataclass(frozen=True, kw_only=True)
class CommsOrderTool(ThreadToolRequest):
    label = "Assign problem order"
    description = (
        "Reserve a Todo assignment and publish its Goal with a deadline; does not replay input."
    )
    order: Annotated[
        OrderGoalAction, *tool_field("Problem, request, expected_revision, deadline, commander")
    ]
    owner: Annotated[str, *tool_field("Assignee thread")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProblemOrdered:
        goal = comms.goals.update_goal(self.owner, self.order.bound_to(
            comms.registry.require(caller.name).incarnation))
        return ProblemOrdered(goal, comms.goals.todos.get(self.order.problem))


@dataclass(frozen=True, kw_only=True)
class CommsTodoCreateTool(CommsOrderTool):
    label = "Create problem"
    description = "Create a problem with its owner, checked order and deadline in one command."
    order: Annotated[
        CreateProblemGoalAction, *tool_field("Typed problem creation and order")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsOrderDoneTool(CommsTodoTool):
    label = "Check delivered order"
    description = "Verify and deliver an open order by its own ID; the unit's Goal is unchanged."
    order_id: Annotated[str, *tool_field("Exact open order ID")]
    delivery: Annotated[
        OrderDelivery,
        *tool_field("A clean commit delivery or an operation verified by the order's check"),
    ]
    claims: Annotated[
        tuple[ProgressClaim, ...],
        *tool_field(
            "Merged, installed or working claims, each with the artifact Core verifies in the order's "
            "checkout; at least one"
        ),
    ]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        ProgressClaim.require_claimed(self.claims, "delivered")
        verified = ProgressClaim.require_all(
            self.delivery.required_claims(self.claims), comms, Path(todo.order.check.directory)
        )
        return DeliverTodo(todo.revision, actor, self.delivery, self.order_id,
                           verified_claims=verified)


@dataclass(frozen=True, kw_only=True)
class CommsOrderCheckTool(CommsTodoTool):
    label = "Correct order check"
    description = (
        "Replace the check of an open order, recording the old check, the new one and "
        "why; only its commander may invoke this tool, and the order keeps its deadline "
        "and strikes."
    )
    check: Annotated[
        OrderCheck, *tool_field("The corrected check: directory, command and expected result")
    ]
    why: Annotated[str, *tool_field("Why the recorded check was wrong")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return CorrectCheckTodo(todo.revision, actor, todo.order.check, self.check, self.why)


@dataclass(frozen=True, kw_only=True)
class CommsOrderServeTool(CommsTodoTool):
    label = "Name order phase"
    description = (
        "Name the plan phase an open order serves; only its commander may invoke this "
        "tool, and the order keeps its id, deadline and strikes."
    )
    serves: Annotated[type[OrderPhase], *tool_field("The plan phase the order serves")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return ServeTodo(todo.revision, actor, self.serves)


@dataclass(frozen=True, kw_only=True)
class CommsProblemCloseTool(CommsTodoTool):
    label = "Close installed problem"
    description = (
        "Record installed application evidence and close only a problem with no open order."
    )
    works: Annotated[
        str, *tool_field("What worked in the app, quoting the order's application run")
    ]
    artifact: Annotated[str, *tool_field("Existing verification artifact file")]

    def operation_for(self, todo: Todo, actor: ThreadIncarnation, comms: Comms) -> TodoOperation:
        return InstallTodo(todo.revision, actor, self.works, self.artifact)

    def result(self, changed: Todo) -> ProblemClosed:
        return ProblemClosed(changed)


@dataclass(frozen=True, kw_only=True)
class CommsTurnsTool(ThreadReadTool):
    label = "Recent turns"
    description = "Read a unit's recent turns with ordered original transcript facts."
    last: Annotated[int, *tool_field("Number of newest turns")] = LastTurns.count

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadTurns:
        return comms.transcripts.turns(caller.subject(self.thread), LastTurns(self.last))


@dataclass(frozen=True, kw_only=True)
class CommsOrderStoryTool(ReadTool, ToolRequest):
    label = "Order story"
    description = (
        "Read a saved order's dated facts from its Todo, oldest first. "
        "Send-back and its implied strike share one reason. "
        "Overwritten order bodies cannot be reconstructed."
    )
    order_id: Annotated[str, *tool_field("Immutable order ID")]

    def apply(self, comms: Comms, caller: ToolCaller) -> OrderStory:
        (story,) = OrderStory.collect(comms.goals.todos.for_order(self.order_id))
        return story


@dataclass(frozen=True, kw_only=True)
class CommsFleetStoryTool(ThreadReadTool):
    label = "Fleet story"
    description = ("Read one story line for every pending order of this commander, "
                   "including linked problems.")

    def apply(self, comms: Comms, caller: ToolCaller) -> FleetStory:
        actor = comms.registry.require(caller.subject(self.thread)).incarnation
        todos = (todo for todo in comms.goals.todos.for_commander(actor) if todo.pending)
        return FleetStory(tuple(story.line for story in OrderStory.collect(todos)))


@dataclass(frozen=True, kw_only=True)
class CommsOrderWorkTool(ReadTool, ToolRequest):
    label = "Order work"
    description = (
        "Read every tool call the problem's assignee made since its order, newest "
        "first, from the unit's own turn records."
    )
    problem: Annotated[str, *tool_field("Problem ID")]

    def apply(self, comms: Comms, caller: ToolCaller) -> OrderWork:
        todo = comms.goals.todos.get(self.problem)
        since = todo.order.at.recorded_at("An order")
        turns = comms.transcripts.turns(todo.held().owner, TurnsSince(since))
        return OrderWork.read(self.problem, turns.thread, since, turns.turns)


@dataclass(frozen=True, kw_only=True)
class CommsThreadsTool(ReadTool, ToolRequest):
    label = "Comms Threads"
    description = (
        "List agent threads with status and pending counts, plus this caller's "
        "recent sent-message delivery outcomes."
    )
    active_only: Annotated[bool, *tool_field("Exclude stopped threads")] = False

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
    checkout: Annotated[
        str, *tool_field("Path of a checkout of the repository (its worktrees name owners)")
    ]
    unlinked_only: Annotated[
        bool, *tool_field("List only branches with no open PR or issue")
    ] = True

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
    action: Annotated[type[RelationshipEdit], *tool_field("Relationship change")]
    peer: Annotated[str, *tool_field("Collaborating agent thread name or alias")]
    note: Annotated[str, *tool_field("Short description of ongoing work")] = (
        ""
    )

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
    new_name: Annotated[str, *tool_field("Your new thread name")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> RenameThreadResult:
        return comms.threads.rename_self(self.new_name, caller=caller)


@dataclass(frozen=True)
class SendRefusalsRead(ToolResult):
    refusals: tuple[SendRefusal, ...]


@dataclass(frozen=True, kw_only=True)
class CommsSendRefusalsTool(ReadTool, ToolRequest):
    label = "Comms Send Refusals"
    description = "Read original refused-send outcomes; reading records no new outcome."
    sender: Annotated[str, *tool_field("Sender to filter; empty reads all senders")] = (
        ""
    )
    target: Annotated[str, *tool_field("Target to filter; empty reads all targets")] = (
        ""
    )
    limit: Annotated[int, *tool_field("Maximum recent refusals, between 1 and 200")] = (
        50
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> SendRefusalsRead:
        return SendRefusalsRead(SendRefusal.recent(
            comms.root, sender=self.sender, target=self.target, limit=self.limit
        ))


@dataclass(frozen=True, kw_only=True)
class CommsSendTool(ToolRequest):
    label = "Comms Send"
    description = (
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text."
    )
    sender: Annotated[str, *tool_field("Sender thread name", wire_name="from")]
    target: Annotated[str, *tool_field("Target thread name or channel", wire_name="to")]
    body: Annotated[str, *tool_field("Message text")]
    audience: Annotated[
        tuple[str, ...], *tool_field("Threads a channel message is for; no other member is woken")
    ] = ()
    type: Annotated[
        Type[MessageKind], *tool_field("Message kind", choices=SendableMessageKind.names)
    ] = InfoKind
    report: Annotated[
        ReportEvidence,
        *tool_field("Change report fields; required for the change-report kind"),
    ] = NoChangeReport()

    def apply(self, comms: Comms, caller: ToolCaller) -> MessagePublished:
        try:
            caller.require_directive(comms, self.body)
            body = VerdictAudience(self.audience).addressed(self.target, self.body)
            result = comms.messaging.send(self.sender, self.target, body, self.type, report=self.report)
        except ToolRefusedError as error:
            # The caller hears the refusal now; the record waits for the wire lock on its own.
            threading.Thread(target=SendRefusal.record,
                             args=(comms.root, self.sender, self.target, error)).start()
            raise
        caller.sent(comms, result.message)
        return MessagePublished.from_send(result)


@dataclass(frozen=True, kw_only=True)
class CommsAuthoredTaskTool(ThreadToolRequest):
    """One original authorized publication path for authored task declarations."""

    target: Annotated[
        str, *tool_field("Thread or channel receiving the original declaration", wire_name="to")
    ]
    scope: Annotated[
        TaskScopeSelection, *tool_field("Current project/goal or explicit scope")
    ] = CurrentTaskScopeSelection()
    change: Annotated[
        TaskChange, *tool_field("Original declaration or correction naming its original reference")
    ] = OriginalTaskChange()

    @abstractmethod
    def declaration(self, owner): ...

    @abstractmethod
    def original_body(self, declaration): ...

    def apply(self, comms: Comms, caller: ThreadCaller) -> TaskPublished:
        owner = comms.registry.require(caller.name)
        declaration = self.declaration(owner)
        result = comms.messaging.send(
            owner.name, self.target, self.original_body(declaration),
            role=NoticeRole(), task=declaration)
        return TaskPublished(result.message.reference, result.message.task, result.correction)


@dataclass(frozen=True, kw_only=True)
class CommsDecisionTool(CommsAuthoredTaskTool):
    label = "Record Decision"
    description = (
        "Record a chosen alternative and valid rejected alternatives on its original "
        "wire message. Author and source turn come from your authorized execution. "
        "Defaults to current project/goal revision or current turn; corrections name "
        "the original reference and grant no execution."
    )
    chosen: Annotated[str, *tool_field("Chosen alternative, preserving exact wording")]
    rejected: Annotated[
        tuple[str, ...], *tool_field("Nonempty unique valid rejected alternatives")
    ]

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
        "is authorized."
    )
    text: Annotated[
        str, *tool_field("Exact authored restriction, never a paraphrase of another source")
    ]

    def declaration(self, owner):
        return Constraint.from_claim(owner, self.scope, self.change)

    def original_body(self, declaration):
        if not self.text.strip():
            raise ToolRefusedError("An authored constraint requires exact nonempty wording")
        return self.text


@dataclass(frozen=True, kw_only=True)
class CommsSubtaskTool(CommsAuthoredTaskTool):
    label = "Record Subtask"
    description = (
        "Explicitly record whether a described subtask is completed or unfinished. "
        "This is not assistant-turn termination or goal completion. Original author, "
        "turn, scope and correction reference use the existing authorized task owner. "
        "A completed observation may permit optional compaction when enabled."
    )
    text: Annotated[str, *tool_field("Exact description of the observed subtask")]
    completed: Annotated[
        bool, *tool_field("Your explicit observation: completed or unfinished")
    ]

    def declaration(self, owner):
        if not self.text.strip():
            raise ToolRefusedError("A subtask observation requires its exact description")
        return Subtask.from_claim(owner, self.scope, self.change, completed=self.completed)

    def original_body(self, declaration):
        return self.text


@dataclass(frozen=True)
class StandbyRequest:
    goal_id: Annotated[str, *tool_field("Current goal for scoped standby review")]
    wait_for: Annotated[
        tuple[str, ...], *tool_field("Declared dependencies for standby review")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsHistoryTool(ReadTool, ToolRequest):
    label = "Conversation history"
    description = (
        "Read one bounded, newest-first channel or DM history page. "
        "Pass before for older messages. Does not acknowledge messages."
    )
    peer: Annotated[str, *tool_field("Channel name or other participant's thread name")]
    participant: Annotated[
        str, *tool_field("Participant reading the conversation; omit to use the caller")
    ] = ""
    limit: Annotated[int, *tool_field("Maximum messages in this page")] = 20
    before: Annotated[
        PageCursor, *tool_field("Exclusive older-page cursor from the previous result")
    ] = NewestCursor()

    def apply(self, comms: Comms, caller: ToolCaller) -> HistoryPageResult:
        target = CommandTarget.named(self.peer)
        return HistoryCliCommand(
            target=target.canonical_name(lambda name: comms.registry.require(name).name),
            me=NamedViewer(caller.subject(self.participant)),
            limit=self.limit, before=self.before,
        ).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsInboxTool(ToolRequest):
    label = "Comms Inbox"
    description = (
        "Fetch one oldest-first page of undelivered messages and unresolved native input "
        "attempts for a thread. Optional target limits the conversation. ACK marks exactly "
        "the returned message references; remaining counts unread messages in that snapshot "
        "after this page's acknowledgment. "
        "Call again to drain the next page. One oversized message is returned alone for "
        "progress. Unresolved inputs remain UNKNOWN and are not paged. For standby, supply standby "
        "(goal_id and wait_for) to get standby_review: exact eligible keys and messages, "
        "already reviewed entries, and excluded owner/other-dependency inputs."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    target: Annotated[str, *tool_field("Channel or peer conversation")] = ANY_CHANNEL.name
    ack: Annotated[bool, *tool_field("Mark exactly the returned messages delivered")] = (
        True
    )
    limit: Annotated[int, *tool_field("Maximum messages in this page; must be positive")] = (
        100
    )
    standby: Annotated[
        StandbyRequest | None, *tool_field("Goal and dependencies to review")
    ] = None

    def apply(self, comms: Comms, caller: ToolCaller) -> InboxPageRead:
        thread = self.thread
        if not (target := self.target.strip()):
            raise ToolRefusedError("An inbox target cannot be empty.")
        standby = self.standby
        review = (
            comms.goals.goal_input_review(thread, standby.goal_id, standby.wait_for)
            if standby is not None
            else None
        )
        page = comms.bus.inbox_at(
            thread, comms.registry.snapshot(), CommandTarget.named(target),
            bounds=PageBounds(limit=self.limit, max_bytes=MAX_INLINE_OUTPUT_BYTES // 2),
        )
        messages = page.page.messages
        unresolved = tuple(comms.goals.unresolved_inputs(thread))
        references = tuple(message.reference for message in messages)
        acknowledged = comms.messaging.acknowledge(thread, references) if self.ack else 0
        return InboxPageRead(
            messages, acknowledged, page.remaining + len(messages) - acknowledged,
            unresolved, standby_review=review,
        )


@dataclass(frozen=True, kw_only=True)
class CommsForkTool(LiveOwnerTool, NativeOwnerCommand, ToolRequest):
    label = "Comms Fork"
    description = "Fork a child agent thread from a registered parent's persistent session."
    context = "thread"
    action_label = "Fork from this thread"
    action_order = 10
    name: Annotated[str, *tool_field("Child thread name")]
    parent: Annotated[str, *tool_field("Parent thread name", binding=SubjectBinding)]
    task: Annotated[str, *tool_field("Optional task for the child")] = ""
    tags: Annotated[
        TagChoice, *tool_field("Comma-separated tags; omitted inherits parent tags")
    ] = InheritedTags()

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadCreatedResult:
        parent = comms.registry.require(self.parent)
        return ForkCliCommand(name=self.name, parent=parent.name, task=self.task,
                              project=parent.worktree, model=parent.model,
                              thinking_level=parent.thinking_level,
                              tags=self.tags).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsStopTool(NativeOwnerCommand, OwnerLifecycleControl, ToolRequest):
    label = "Stop Comms Thread"
    description = "Stop a thread after verifying that its process owns the registered identity."
    context = "thread"
    action_label = "Stop process"
    action_order = 20
    name: Annotated[str, *tool_field("Thread name", binding=SubjectBinding)]

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
    name: Annotated[str, *tool_field("Thread to start", binding=SubjectBinding)]

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
    name: Annotated[str, *tool_field("Live agent thread", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartQueued:
        return RestartQueued(
            enqueue_restart(comms, OwnerRestartSelection.capture(comms.registry.snapshot(), self.name),
                            launch_runtime=LaunchRuntime.running()))


@dataclass(frozen=True, kw_only=True)
class CommsRestartQueueTool(ReadTool, ToolRequest):
    label = "Restart Queue Status"
    description = "Inspect queued, restarted, stale, or uncertain attempts for a thread."
    name: Annotated[str, *tool_field("Agent thread")]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartAttempts:
        return RestartAttempts(tuple(restart_status(comms, self.name)))


@dataclass(frozen=True, kw_only=True)
class CommsCancelRestartTool(ToolRequest):
    label = "Cancel Queued Restart"
    description = "Cancel pending restarts for a thread; cannot undo an attempted restart."
    name: Annotated[str, *tool_field("Agent thread")]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartsCancelled:
        return RestartsCancelled(tuple(cancel_restart(comms, self.name)))


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(ArchiveOwnerControl, ToolRequest):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    context = "thread"
    action_label = "Archive stopped thread"
    action_order = 30
    name: Annotated[str, *tool_field("Stopped thread name", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadArchivedResult:
        return ArchiveCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class ToolsCliCommand(CliCommand):
    help = "Emit the shared adapter tool catalog"
    name: Annotated[
        str | None, Argument(flags=("--name",), options={"help": "Calling thread"})
    ] = None

    def apply(self, ctx: Comms) -> ToolCatalogResult:
        caller = ToolCaller.from_actor(self.name)
        return caller.catalog(ctx, tuple(NativeRequestTools.catalog()))


@dataclass(frozen=True, kw_only=True)
class InvokeCliCommand(CliCommand, declared_name="tool"):
    help = "Invoke one declared adapter tool, as --from THREAD when given"
    caller: Annotated[
        str | None,
        Argument(
            flags=("--from",),
            options={"help": "Calling thread; omit for tools that need no thread"},
        ),
    ] = None
    tool: Annotated[str, Argument(flags=("--tool",))]
    arguments: Annotated[
        dict[str, object],
        Argument(
            flags=("--arguments",),
            options={"help": "Tool arguments as a JSON object"},
        ),
    ] = dataclasses.field(default_factory=dict)

    def argv(self, root: Path) -> tuple[str, ...]:
        """The CLI boundary owns its module, grammar and argument serialization."""
        caller = ToolCaller.from_actor(self.caller)
        return (sys.executable, '-m', comms_cli.__name__, '--root', str(root),
                self.declared_name, '--from', caller.acting_name(''), '--tool', self.tool,
                '--arguments', json.dumps(self.arguments))

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
    return ToolRequest.member_named(name).invoke(comms, arguments, caller=caller)


def invoke_context_tool(
    comms: Comms,
    name: str,
    *,
    subject: str,
    actor: str | None,
    arguments: Mapping[str, object] | None = None,
) -> dict[str, JsonValue]:
    tool = ToolRequest.member_named(name)
    if tool.context is None:
        raise ToolRefusedError(f"Tool {name!r} is not a context action.")
    bound = dict(arguments or {})
    bound.update(
        (key, binding.resolve(subject, actor)) for key, binding in tool.context_bindings().items()
    )
    return tool.invoke(comms, bound, caller=ToolCaller.from_actor(actor))
