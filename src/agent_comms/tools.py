"""Named external tool requests; declarations own schema, context and behavior."""

from __future__ import annotations

from .input_key import InputKeyText

from .tool_request import ListedNames, LiveOwnerTool, OmittedNames, PublishingTool, PublishingToolRequest, ReadTool, SubjectBinding, ThreadReadTool, ThreadToolRequest, ToolRequest, tool_field
from .order_tools import MessagePublished

import dataclasses
import json
import sys
import threading
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Type

from pydantic import JsonValue

from agent_comms.lifecycle_capability import Active

from . import cli as comms_cli
from .change_report import NoChangeReport, ReportEvidence
from .channel_management import ListTagEdit, TagEdit
from agent_comms.channel_targets import ChannelTarget, CommandTarget, NamedTarget
from .channels import AnyChannel, Channel, SavedView, ViewMatch, ViewPredicate
from .cli_commands import (
    ArchiveCliCommand,
    CliCommand,
    DeleteViewCliCommand,
    ForkCliCommand,
    HistoryCliCommand,
    PinChannelCliCommand,
    PinThreadCliCommand,
    QueueRestartCliCommand,
    StartCliCommand,
    StopCliCommand,
    ThreadArchivedResult,
    ThreadCreatedResult,
    ThreadsCliCommand,
    ThreadStoppedResult,
)
from .cli_result import CliResult, ToolCatalogResult
from .coordination_tables.refusals import SendRefusal
from .display_order import ChannelSort, ThreadSort
from .errors import RelationViolationError, ToolRefusedError
from .field_codec import (
    Argument,
    FieldCodec,
    Normalize,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    WireForm,
    WireName,
    WireOmitDefault,
    projected,
    GoalId,
)
from .goal_actions import ActiveGoalAction, EditGoalAction, GoalAction, GoalPrecondition, ModelInvocable, SetGoalAction, TransitionGoalAction
from .goal_history import RecordedGoalHistory
from .goal_presentation import GoalExecution, GoalSnapshot
from .goal_waits import NoStandbyReview, StandbyReview
from .goals import GoalDeclaration
from .history_result import HistoryPageResult
from .input_attempt import UnresolvedInput
from .message_page import PageBounds
from .message_reference import MessageReference
from .messages import InfoKind, Message, MessageKind, NoticeRole, SendableMessageKind
from .native_tools import NativeRequestTools
from .owner_lifecycle import OwnerStartResult
from .page_traversal import NewestCursor, PageCursor
from .pi_payloads import PublishedGoalToolDetails
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .presentation import ChannelView
from .progress_claims import ProgressClaim
from .read_basis import NamedViewer
from .relationships import (
    Collaboration,
    GoalDerivedContact,
    GoalMentionDiagnostic,
    RelationshipEdit,
)
from .restart_queue import (
    QueuedRestart,
)
from .restart_queue import (
    cancel as cancel_restart,
)
from .restart_queue import (
    RestartQueued,
)
from .restart_queue import (
    status as restart_status,
)
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
from .tool_caller import NoThreadCaller, ThreadCaller, ThreadListing, ToolCaller
from .tool_result import ToolResult
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
class GoalHistory(ToolResult, RecordedGoalHistory):
    pass












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
    """One unread page; only its returned references can be acknowledged."""

    messages: tuple[Annotated[Message, WireForm], ...]
    acknowledged: int
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
        ProgressClaim.require_all(self.claims, comms, Path(comms.registry.require(name).worktree))
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
                    goal_id=goal_id,
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
                expect=GoalPrecondition(expected_goal=current, goal_id=goal_id), text=self.text
            ),
        )
        return GoalUpdated(goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalHistoryTool(ThreadReadTool):
    label = "Goal history"
    description = "Read recorded goal revisions and recorded provenance and observation gaps."
    goal_id: Annotated[
        GoalId | None, *tool_field("Optional goal ID; omit for this thread's full history")
    ] = None

    def apply(self, comms: Comms, caller: ToolCaller) -> GoalHistory:
        return comms.goals.recorded_history(
            GoalHistory, caller.subject(self.thread), goal_id=self.goal_id
        )














































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
class SendTool(PublishingTool[ThreadCaller]):
    label = "Comms Send"
    description = (
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text."
    )
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

    def publish(self, comms: Comms, caller: ThreadCaller) -> MessagePublished:
        sender = caller.own_thread()
        target = CommandTarget.named(self.target)
        try:
            caller.require_directive(comms, self.body)
            body = target.addressed(self.audience, self.body)
            result = comms.messaging.send(
                sender, target.name, body, self.type, report=self.report,
                task=self.attachment(comms, target), origin=caller.origin())
        except ToolRefusedError as error:
            # The caller hears the refusal now; the record waits for the wire lock on its own.
            threading.Thread(target=SendRefusal.record,
                             args=(comms.root, sender, self.target, error)).start()
            raise
        return MessagePublished.from_send(result)

    def attachment(self, comms: Comms, target: NamedTarget) -> TaskAttachment:
        """An ordinary send carries no authored task or selected request."""
        return NoTaskAttachment()


class CommsSendTool(SendTool, ThreadToolRequest):
    """The native send declaration; its fields and publication belong to SendTool."""


@dataclass(frozen=True, kw_only=True)
class CommsAuthoredTaskTool(ThreadToolRequest, PublishingToolRequest[ThreadCaller]):
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

    def publish(self, comms: Comms, caller: ThreadCaller) -> TaskPublished:
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
    goal_id: Annotated[GoalId, *tool_field("Current goal for scoped standby review")]
    wait_for: Annotated[
        tuple[str, ...], *tool_field("Declared dependencies for standby review")
    ]

    def review(self, comms: Comms, thread: str) -> StandbyReview:
        return comms.goals.goal_input_review(thread, self.goal_id, self.wait_for)


@dataclass(frozen=True)
class NoStandby(NullMember):
    """The inbox read asked for no standby review."""

    def review(self, comms: Comms, thread: str) -> NoStandbyReview:
        return NoStandbyReview()


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
    target: Annotated[str, *tool_field("Channel or peer conversation")] = AnyChannel.address()
    ack: Annotated[bool, *tool_field("Mark exactly the returned messages delivered")] = (
        True
    )
    limit: Annotated[int, *tool_field("Maximum messages in this page; must be positive")] = (
        100
    )
    standby: Annotated[
        StandbyRequest | NoStandby, *tool_field("Goal and dependencies to review")
    ] = NoStandby()

    def apply(self, comms: Comms, caller: ToolCaller) -> InboxPageRead:
        thread = self.thread
        if not (target := self.target.strip()):
            raise ToolRefusedError("An inbox target cannot be empty.")
        review = self.standby.review(comms, thread)
        page = comms.bus.inbox_at(
            thread, comms.registry.snapshot(), CommandTarget.named(target),
            bounds=PageBounds(limit=self.limit),
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
class CommsForkTool(LiveOwnerTool, NativeOwnerCommand, ToolRequest):
    label = "Comms Fork"
    description = "Fork a child agent thread from a registered parent's persistent session."
    context = "thread"
    action_label = "Fork from this thread"
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
    name: Annotated[str, *tool_field("Live agent thread", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartQueued:
        return QueueRestartCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(ArchiveOwnerControl, ToolRequest):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    context = "thread"
    action_label = "Archive stopped thread"
    name: Annotated[str, *tool_field("Stopped thread name", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadArchivedResult:
        return ArchiveCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class ToolsCliCommand(CliCommand):
    help = "Emit the shared adapter tool catalog"
    name: Annotated[
        ThreadCaller | NoThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(flags=("--name",), options={"type": ThreadCaller, "help": "Calling thread"}),
    ] = NoThreadCaller()

    def apply(self, ctx: Comms) -> ToolCatalogResult:
        return self.name.catalog(ctx, tuple(NativeRequestTools.catalog()))


@dataclass(frozen=True)
class ToolReplyResult(CliResult):
    """The tool protocol's own reply, the one every adapter receives, written unwrapped."""

    reply: dict[str, JsonValue]

    def to_wire(self) -> dict[str, JsonValue]:
        return self.reply


@dataclass(frozen=True, kw_only=True)
class InvokeCliCommand(CliCommand[ToolReplyResult], declared_name="tool"):
    help = "Invoke one declared adapter tool, as --from THREAD when given"
    caller: Annotated[
        ThreadCaller | NoThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--from",),
            options={"type": ThreadCaller, "help": "Calling thread; omit for tools that need no thread"},
        ),
    ] = NoThreadCaller()
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
        return (sys.executable, '-m', comms_cli.__name__, '--root', str(root),
                self.declared_name, *self.caller.from_flag(), '--tool', self.tool,
                '--arguments', json.dumps(self.arguments))

    def apply(self, ctx: Comms) -> ToolReplyResult:
        return ToolReplyResult(invoke_tool(ctx, self.tool, self.arguments, caller=self.caller))


def invoke_tool(
    comms: Comms, name: str, arguments: Mapping[str, object], *, caller: ToolCaller
) -> dict[str, JsonValue]:
    return ToolRequest.member_named(name).invoke(comms, arguments, caller=caller)
