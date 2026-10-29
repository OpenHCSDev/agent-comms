"""Named external tool requests; declarations own schema, context and behavior."""

from __future__ import annotations

from .input_key import InputKeyText
from .agent_inbox import TakenInputStart

from .tool_request import ListedNames, LiveOwnerTool, OmittedNames, PublishingTool, PublishingToolRequest, ReadTool, SubjectBinding, ThreadReadTool, ThreadToolRequest, ToolRequest, tool_field
from .order_tools import MessagePublished

import dataclasses
import threading
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Type

from pydantic import JsonValue

from agent_comms.lifecycle_capability import Active

from .change_report import NoChangeReport, ReportEvidence
from .channel_management import ListTagEdit, TagEdit
from agent_comms.channel_targets import ChannelTarget, CommandTarget, NamedTarget
from .channels import AnyChannel, Channel, SavedView, ViewMatch, ViewPredicate
from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_verbs.delete_view import DeleteViewCliCommand
from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.cli_verbs.history import HistoryCliCommand
from agent_comms.cli_verbs.pin_channel import PinChannelCliCommand
from agent_comms.cli_verbs.pin_thread import PinThreadCliCommand
from agent_comms.cli_verbs.queue_restart import QueueRestartCliCommand
from agent_comms.cli_verbs.start import StartCliCommand
from agent_comms.cli_verbs.stop import StopCliCommand
from agent_comms.cli_commands import ThreadArchivedResult, ThreadCreatedResult, ThreadStoppedResult
from agent_comms.cli_verbs.threads import ThreadsCliCommand
from .cli_result import CliResult
from .coordination_tables.refusals import SendRefusal
from .display_order import ChannelSort, ThreadSort
from .errors import RelationViolationError, ToolRefusedError
from .field_codec import FieldCodec, NullMember, SingleFieldValue, SubclassUnion, WireForm, WireName, WireOmitDefault, projected, GoalId
from .goal_actions import ActiveGoalAction, EditGoalAction, GoalAction, GoalPrecondition, ModelInvocable, SetGoalAction, TransitionGoalAction
from .goal_history import RecordedGoalHistory
from .goal_scope import AllGoals, OneGoal
from .goal_presentation import GoalExecution, GoalSnapshot
from .goal_waits import NoStandbyReview, StandbyReview
from .goals import GoalDeclaration
from .history_result import HistoryPageResult
from .input_attempt import UnresolvedInput
from .message_page import PageBounds
from .message_reference import MessageReference
from .messages import InfoKind, Message, MessageKind, NoticeRole, SendableMessageKind
from .owner_lifecycle import OwnerStartResult
from .owner_connection import OwnerConnection
from .owner_launch import HostResources, ReadHostResourcesRuntimeRequest
from .page_traversal import NewestCursor, PageCursor
from .pi_payloads import PublishedGoalToolDetails
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .presentation import AfterRecipient, ChannelView, FirstRecipient, MessageNotification, RecipientCursor
from .notification_evidence import RecipientSelection
from .progress_claims import ProgressClaim
from .read_basis import NamedViewer
from .relationships import (
    Collaboration,
    GoalDerivedContact,
    GoalMentionDiagnostic,
    RelationshipEdit,
)
from .restart_queue import QueuedRestart, RestartQueued, RestartQueue
from .send_result import CorrectionResult
from .task_sources import (
    Constraint,
    CurrentTaskScopeSelection,
    NoTaskAttachment,
    Decision,
    OriginalTaskChange,
    Subtask,
    TaskAttachment,
    TaskChange,
    TaskScopeSelection,
)
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .progress_claims import ProgressClaim
from .relationships import RelationshipEdit
from .thread_execution import ArchiveOwnerControl, NativeOwnerCommand, OwnerLifecycleControl, StartOwnerControl
from .thread_management import InheritedTags, RenameThreadResult, TagChoice
from .thread_model import ThreadModel
from .threads import NextTurnConfiguration
from .tool_caller import ThreadCaller, ThreadListing, ToolCaller
from .tool_result import ToolResult
from .notification_reads import LiveNotificationRead
from .work_branches import WorkBranch, WorkBranches

if TYPE_CHECKING:
    from .comms import Comms




@dataclass(frozen=True)
class ModelChanged(ToolResult):
    thread: str
    model: ThreadModel
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
    execution: Annotated[GoalExecution, WireName("goal_execution")]


@dataclass(frozen=True)
class GoalUpdated(OwnGoalPublication):
    goal: GoalDeclaration


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
class TaskPublished(ToolResult):
    reference: MessageReference
    task: TaskAttachment
    correction: CorrectionResult


@dataclass(frozen=True)
class InboxPageRead(ToolResult):
    """One page of messages nothing took yet; reading it records nothing."""

    messages: tuple[Annotated[Message, WireForm], ...]
    remaining: int
    unresolved_inputs: tuple[UnresolvedInput, ...]
    standby_review: Annotated[StandbyReview | NoStandbyReview, WireOmitDefault()] = (
        NoStandbyReview()
    )


@dataclass(frozen=True)
class RestartAttempts(ToolResult):
    restarts: tuple[QueuedRestart, ...]


@dataclass(frozen=True)
class RestartsCancelled(ToolResult):
    cancelled: tuple[QueuedRestart, ...]


























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
    edit: Annotated[TagEdit, *tool_field("Tag operation and its arguments")] = (
        dataclasses.field(default_factory=ListTagEdit)
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> TagList:
        tags = self.edit.apply(comms.channels)
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
    kind: Annotated[type[SavedView], *tool_field("Projection kind")]
    match: Annotated[type[ViewMatch], *tool_field("Tag predicate")]
    tags: Annotated[str, *tool_field("One or more comma-separated tags")]

    def apply(self, comms: Comms, caller: ToolCaller) -> SavedView:
        return comms.channels.set_saved_view(
            self.kind(self.name, ViewPredicate(self.match, _tag_set(self.tags))))


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
    goal_id: Annotated[GoalId, *tool_field("Goal identity provided in the turn context")]
    status: Annotated[
        type[TransitionGoalAction], *tool_field("Goal state", choices=GoalAction.model_choices)
    ]
    progress: Annotated[
        str, *tool_field("Progress summary; blocked requires a nonempty explicit reason")
    ]
    wait_for: Annotated[
        ListedNames | OmittedNames, *tool_field("Explicit thread names or @names; required for standby")
    ] = OmittedNames()
    reviewed_inputs: Annotated[
        ListedNames | OmittedNames,
        *tool_field(
            "Exact unresolved inputId keys inspected and handled for this goal; standby only"
        ),
    ] = OmittedNames()
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
        ProgressClaim.require_all(self.claims, comms, comms.registry.require(name).worktree.path)
        command = replace(
            self.status(progress=self.progress),
            expect=self.status.model_precondition(self.goal_id),
            **{key: names for key, names in (
                ("wait_for", self.wait_for.names), ("reviewed_inputs", tuple(
                    InputKeyText.from_text(key) for key in self.reviewed_inputs.names
                )),
            ) if names},
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
    goal_id: Annotated[GoalId, *tool_field("Identity of the paused goal to resume")]
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
                    goal_id=OneGoal(goal_id),
                ),
                progress=progress,
            ),
        )
        goal = goal.require()
        if not goal.state.items_with(Active, goal) or goal.progress != progress:
            raise ToolRefusedError("Goal changed during resume; refresh its state.")
        return GoalUpdated(goal)


@dataclass(frozen=True, kw_only=True)
class CommsEditGoalTool(ThreadToolRequest):
    label = "Edit goal text"
    description = (
        "Edit your existing goal text while keeping its identity, status, and progress."
        " Use comms_set_goal only to replace the objective with a fresh goal ID."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the existing goal to edit")]
    text: Annotated[str, *tool_field("Revised objective text, including any @mentions")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> GoalUpdated:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).goal.require().require_id(goal_id)
        goal = comms.goals.update_goal(
            name,
            EditGoalAction(
                expect=GoalPrecondition(expected_goal=current, goal_id=OneGoal(goal_id)), text=self.text
            ),
        )
        return GoalUpdated(goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalHistoryTool(ThreadReadTool):
    label = "Goal history"
    description = "Read recorded goal revisions and recorded provenance and observation gaps."
    goal_id: Annotated[
        OneGoal | AllGoals, *tool_field("Optional goal ID; omit for this thread's full history")
    ] = AllGoals()

    def apply(self, comms: Comms, caller: ToolCaller) -> RecordedGoalHistory:
        return comms.goals.recorded_history(caller.subject(self.thread), self.goal_id)














































@dataclass(frozen=True, kw_only=True)
class CommsThreadsTool(ReadTool, ToolRequest):
    label = "Comms Threads"
    description = (
        "List agent threads with status and pending counts, plus one delivery summary "
        "for each of this caller's recent sent messages; comms_message_outcomes reads one "
        "message's recipients."
    )
    active_only: Annotated[bool, *tool_field("Exclude stopped threads")] = False

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadListing:
        threads = ThreadsCliCommand(active_only=self.active_only).apply(comms)
        return caller.listing(comms, threads.threads)


@dataclass(frozen=True)
class MessageOutcomes(ToolResult):
    """One message once, then each recipient outcome its caller may read."""

    message: Annotated[Message, WireForm]
    outcomes: tuple[MessageNotification, ...]
    remaining: int
    next: RecipientCursor


@dataclass(frozen=True, kw_only=True)
class CommsMessageOutcomesTool(ReadTool, ToolRequest):
    label = "Message outcomes"
    description = (
        "Read one message's recipient outcomes: the message once, then one page of recipient "
        "rows, each with its delivery state, who it was shown to and its answer. Pass the "
        "result's next cursor as after for the next page. A sender reads every recipient; "
        "a recipient reads only its own row."
    )
    reference: Annotated[
        MessageReference, *tool_field("The message's seq and message_id, as comms_threads lists them")
    ]
    limit: Annotated[int, *tool_field("Maximum recipient rows in this page")] = RecipientSelection.max_limit
    after: Annotated[
        RecipientCursor, *tool_field("The previous page's next cursor; omit for the first page")
    ] = FirstRecipient()

    def apply(self, comms: Comms, caller: ToolCaller) -> MessageOutcomes:
        try:
            RecipientSelection.require_limit(self.limit)
        except ValueError as error:
            raise ToolRefusedError(str(error)) from error
        source, = comms.bus.bus.deliveries_for_references((self.reference,))
        if not caller.outcome_viewer(comms).sees(source):
            raise ToolRefusedError(f"Message {source.message.seq} is not readable by this caller.")
        window = LiveNotificationRead((self.reference,), after=self.after, limit=self.limit).read_as(comms, caller)
        page = window.outcomes.get((source.message.seq, source.message.message_id), ())
