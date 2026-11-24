"""Wire declarations for standalone Comms tool commands and reads.

Serving these declarations lives in agent_comms.transport.served_tools.served, which only the
server imports.
"""

from __future__ import annotations

import dataclasses
from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, ClassVar

from agent_comms.coordination.tables.refusals import SendRefusal
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.pi_vocabulary import ServiceTier, ThinkingLevelField
from agent_comms.messaging.channels import Channel, SavedView, ViewMatch
from agent_comms.messaging.history_result import HistoryPageResult
from agent_comms.messaging.message_notification import MessageNotification
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.messaging.notification_evidence import (
    FirstRecipient,
    RecipientCursor,
    RecipientLimit,
)
from agent_comms.messaging.page_traversal import NewestCursor, PageCursor
from agent_comms.messaging.send_result import CorrectionResult
from agent_comms.messaging.tag_edits import ListTagEdit, TagEdit
from agent_comms.messaging.task_sources import (
    OriginalTaskChange,
    TaskAttachment,
    TaskChange,
)
from agent_comms.nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    ContextSource,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    WireForm,
    WireName,
    WireOmitDefault,
)
from agent_comms.planning.task_scope import CurrentTaskScopeSelection, TaskScopeSelection
from agent_comms.presentation import ChannelView
from agent_comms.projects.work_branches import WorkBranch
from agent_comms.registry.relationship_store import Collaboration, GoalDerivedContact
from agent_comms.registry.relationships import GoalMentionDiagnostic, RelationshipEdit
from agent_comms.registry.tag_choice import InheritedTags, TagChoice
from agent_comms.registry.thread_identity import ThreadIncarnation, ToolName
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.registry.thread_results import (
    ProjectChangeResult,
    RenameThreadResult,
    ThreadTitleResult,
)
from agent_comms.registry.threads import NextTurnConfiguration
from agent_comms.transport.host_logs import HostLog, HostLogRead
from agent_comms.transport.request import (
    ReadRequest,
    SubjectBinding,
    ThreadReadRequest,
    ToolRequest,
    tool_field,
)
from agent_comms.transport.tool_caller import ThreadListing, ToolCaller
from agent_comms.transport.tool_declaration import NativeRequestTools
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools.channels import ChannelListing


@dataclass(frozen=True)
class ModelChanged(ToolResult):
    thread: str
    model: ThreadModel
    thinking_level: ThinkingLevelField
    service_tier: ServiceTier


@dataclass(frozen=True)
class TagList(ToolResult):
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ThreadTags(ToolResult):
    thread: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ProjectSet(ProjectChangeResult):
    instruction: str


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
class AvailableTool:
    name: ToolName
    label: str
    description: str


@dataclass(frozen=True)
class ToolsSelected(ToolResult):
    tools: tuple[AvailableTool, ...]


class ToolLookup(DeclaredFamily, affix="ToolLookup"):
    """An exact selection and a word search are distinct ways to acquire declarations."""

    @abstractmethod
    def selected(
        self, catalog: tuple[type[NativeRequestTools], ...],
    ) -> tuple[type[NativeRequestTools], ...]: ...


@dataclass(frozen=True)
class NamedToolLookup(ToolLookup, declared_name="named"):
    tools: Annotated[tuple[ToolName, ...], WireName("names")]

    def selected(
        self, catalog: tuple[type[NativeRequestTools], ...],
    ) -> tuple[type[NativeRequestTools], ...]:
        members = {ToolName(tool.declared_name): tool for tool in catalog}
        unknown = set(self.tools) - members.keys()
        if unknown:
            raise UnknownFamilyMember(
                f"Unknown native tools: {', '.join(sorted(name.value for name in unknown))}"
            )
        return tuple(members[name] for name in dict.fromkeys(self.tools))


@dataclass(frozen=True)
class SearchToolLookup(ToolLookup, declared_name="search"):
    query: str

    def selected(
        self, catalog: tuple[type[NativeRequestTools], ...],
    ) -> tuple[type[NativeRequestTools], ...]:
        terms = self.query.casefold().split()
        return tuple(
            tool
            for tool in catalog
            if all(
                term in f"{tool.declared_name} {tool.label} {tool.description}".casefold()
                for term in terms
            )
        )


@dataclass(frozen=True, kw_only=True)
class CommsDiscoverTool(ReadRequest[ToolsSelected]):
    """The native adapter activates these declarations through Pi's existing tool loader."""

    label = "Find and load Comms tools"
    description = (
        "Find and load any Comms operation before calling it. Search by words in its name "
        "or description, or select exact names. An explicitly empty search loads every tool. "
        "Tools stay active for this turn, not for the session's lifetime. "
        "Use this for messages, inbox, goals, orders, reviews, threads, channels and configuration."
    )
    selection: Annotated[ToolLookup, *tool_field("Search words or select exact tool names")]


@dataclass(frozen=True, kw_only=True)
class CommsSetThreadTitleTool(ToolRequest[ThreadTitleResult]):
    """The owner's UI corrects a managed thread's label without renaming it."""

    label = "Set managed thread title"
    description = (
        "Correct a managed thread's display title while its name and routing aliases stay"
        " unchanged; requires a human UI caller."
    )
    thread: Annotated[ThreadIncarnation, *tool_field("The thread incarnation the UI holds")]
    title: Annotated[str, *tool_field("The new display title")]
    owner: Annotated[ProcessIdentity, *tool_field("The process that manages the thread")]


@dataclass(frozen=True, kw_only=True)
class CommsPinChannelTool(ToolRequest[Channel]):
    label = "Pin Channel"
    description = (
        "Persist whether a channel is pinned. Pinned channels sort before unpinned "
        "channels; the selected channel sort remains the secondary order."
    )
    name: Annotated[str, *tool_field("Channel name")]
    pinned: Annotated[bool, *tool_field("True to pin; false to unpin")]


@dataclass(frozen=True, kw_only=True)
class CommsPinThreadTool(ToolRequest[ChannelView]):
    label = "Pin Thread in Channel"
    description = (
        "Persist whether a member is pinned within one channel. Pins do not change "
        "membership or ordering in other channels. The selected member sort remains the"
        " secondary order."
    )
    channel: Annotated[str, *tool_field("Channel containing the thread")]
    name: Annotated[str, *tool_field("Member thread name")]
    pinned: Annotated[bool, *tool_field("True to pin; false to unpin")]


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
class CommsModelTool(NextTurnConfiguration, ToolRequest[ModelChanged]):
    label = "Change Thread Model"
    description = (
        "Change this thread's model, or another thread's model, for its future turns, "
        "or switch between a native Pi model and CLI inbox (`cli-inbox`). "
        "Optionally change its thinking level and provider service tier too. Applies "
        "from that thread's next turn."
    )
    thread: Annotated[
        ToolSubject,
        *tool_field("Thread to change (defaults to the executing thread)", ContextSource(SubjectBinding)),
    ] = CallerSubject()


@dataclass(frozen=True, kw_only=True)
class CommsTagsTool(ToolRequest[TagList]):
    label = "Manage tags"
    description = (
        "List, create, rename, or delete tags. Renaming/deleting updates thread "
        "assignments and channel filters. Uncovered tags automatically have a channel."
    )
    edit: Annotated[TagEdit, *tool_field("Tag operation and its arguments")] = (
        dataclasses.field(default_factory=ListTagEdit)
    )


@dataclass(frozen=True, kw_only=True)
class CommsThreadTagsTool(ToolRequest[ThreadTags]):
    label = "Assign thread tags"
    description = (
        "Add/remove tags on a thread. Omit thread to manage your own tags. Channel "
        "membership and routing update immediately without restarting or creating an "
        "executor."
    )
    thread: Annotated[str, *tool_field("Thread name or alias")] = ""
    add: Annotated[str, *tool_field("Comma-separated tags to add")] = ""
    remove: Annotated[str, *tool_field("Comma-separated tags to remove")] = ""


@dataclass(frozen=True, kw_only=True)
class CommsSetChannelMetadataTool(ToolRequest[Channel]):
    label = "Set channel presentation"
    description = (
        "Set presentation-only parent and archive state. These fields never alter "
        "routing, membership, history, unread state, or delivery."
    )
    name: Annotated[str, *tool_field("Exact channel name")]
    parent: Annotated[str, *tool_field("Parent channel; empty clears the parent")]
    archived: Annotated[bool, *tool_field("Whether to hide from active presentation")]


@dataclass(frozen=True, kw_only=True)
class CommsSetViewTool(ToolRequest[SavedView]):
    label = "Save view"
    description = (
        "Create or update a saved view filtered by tags. Views cannot receive"
        " messages and never own channel history."
    )
    name: Annotated[str, *tool_field("View name without #")]
    kind: Annotated[type[SavedView], *tool_field("View kind")]
    match: Annotated[type[ViewMatch], *tool_field("Tag match rule")]
    tags: Annotated[str, *tool_field("One or more comma-separated tags")]


@dataclass(frozen=True, kw_only=True)
class CommsDeleteViewTool(ToolRequest[ChannelListing]):
    label = "Delete view"
    description = "Delete a saved view without changing tags, channels, messages, or threads."
    name: Annotated[str, *tool_field("Saved view name")]


@dataclass(frozen=True, kw_only=True)
class CommsSetProjectTool(ToolRequest[ProjectSet]):
    label = "Change Project"
    description = (
        "Change your own persistent project directory and synchronize attached clients."
        " Accepts an existing absolute path, ~ path, or path relative to your current "
        "project. Preserves this thread and its conversation. After changing, end your "
        "turn; the runtime automatically continues in the new project."
    )
    path: Annotated[str, *tool_field("New project directory")]


@dataclass(frozen=True, kw_only=True)
class CommsThreadsTool(ReadRequest[ThreadListing]):
    label = "Comms Threads"
    description = (
        "List agent threads with status and pending counts, plus one delivery summary "
        "for each of this caller's recent sent messages; comms_message_outcomes reads one "
        "message's recipients."
    )
    active_only: Annotated[bool, *tool_field("Exclude stopped threads")] = False


@dataclass(frozen=True)
class MessageOutcomes(ToolResult):
    """One message once, then each recipient outcome its caller may read."""

    message: Annotated[Message, WireForm]
    outcomes: tuple[MessageNotification, ...]
    remaining: int
    next: RecipientCursor


@dataclass(frozen=True, kw_only=True)
class CommsMessageOutcomesTool(ReadRequest[MessageOutcomes]):
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
    limit: Annotated[RecipientLimit, *tool_field("Maximum recipient rows in this page")] = RecipientLimit()
    after: Annotated[
        RecipientCursor, *tool_field("The previous page's next cursor; omit for the first page")
    ] = FirstRecipient()


@dataclass(frozen=True, kw_only=True)
class CommsResourcesTool(ThreadReadRequest[ToolResult]):
    label = "Host resources"
    description = (
        "Read a running thread's shared Python host memory, module and owner counts, "
        "and cumulative registry read/capture/decode counts. Native model children "
        "are separate from this shared host measurement."
    )


@dataclass(frozen=True, kw_only=True)
class CommsHostLogTool(ReadRequest[HostLogRead]):
    label = "Host log"
    description = (
        "Read the host's connector journal, latest install diagnostics or busiest processes, with "
        "secrets hidden: a session on another machine reads a failure itself instead of asking "
        "someone on the host."
    )
    longest: ClassVar[int] = 500

    source: Annotated[type[HostLog], *tool_field("What to read")]
    lines: Annotated[int, *tool_field("How many lines, newest last; from 1 to 500")] = 100


@dataclass(frozen=True, kw_only=True)
class CommsWorkBranchesTool(ReadRequest[WorkBranchList]):
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


@dataclass(frozen=True, kw_only=True)
class CommsCollaborationTool(ToolRequest[CollaborationEdited]):
    label = "Manage mutual collaboration"
    description = (
        "Create, update or end one mutual contact with another agent. Either "
        "participant can add, change its shared note, or remove it from both "
        "collaboration lists. This persistent metadata does not message, wake or fork "
        "either agent."
    )
    action: Annotated[type[RelationshipEdit], *tool_field("Relationship change")]
    peer: Annotated[str, *tool_field("Collaborating agent thread name or alias")]
    note: Annotated[str, *tool_field("Short description of ongoing work")] = ""


@dataclass(frozen=True, kw_only=True)
class CommsCollaborationsTool(ThreadReadRequest[CollaborationList]):
    label = "List collaborations"
    description = (
        "Read explicit contacts and goal-derived awareness with provenance, without "
        "changing delivery, work acceptance or owners."
    )


@dataclass(frozen=True, kw_only=True)
class CommsRenameSelfTool(ToolRequest[RenameThreadResult]):
    label = "Rename Comms Thread"
    description = (
        "Rename your own persistent thread to a new display name; the thread name is that"
        " text's slug and old names remain routing aliases."
    )
    display_name: Annotated[str, *tool_field("Your new display name")]


@dataclass(frozen=True, kw_only=True)
class CommsSetOwnThreadTitleTool(ToolRequest[ThreadTitleResult]):
    """A thread corrects its own display title without touching its identity."""

    label = "Set Comms Thread Title"
    description = (
        "Correct your own thread's display title; its name and routing aliases stay unchanged."
    )
    title: Annotated[str, *tool_field("Your new display title")]


@dataclass(frozen=True)
class SendRefusalsRead(ToolResult):
    refusals: tuple[SendRefusal, ...]


@dataclass(frozen=True, kw_only=True)
class CommsSendRefusalsTool(ReadRequest[SendRefusalsRead]):
    label = "Comms Send Refusals"
    description = "Read original refused-send outcomes; reading records no new outcome."
    sender: Annotated[str, *tool_field("Sender to filter; empty reads all senders")] = ""
    target: Annotated[str, *tool_field("Target to filter; empty reads all targets")] = ""
    limit: Annotated[int, *tool_field("Maximum recent refusals, between 1 and 200")] = 50


@dataclass(frozen=True, kw_only=True)
class AuthoredTaskRequest(ToolRequest[TaskPublished], declared_name=None):
    """Shared wire arguments for authored task declarations."""

    target: Annotated[
        str, *tool_field("Thread or channel receiving the original declaration", WireName("to"))
    ]
    scope: Annotated[
        TaskScopeSelection, *tool_field("Current project/goal or explicit scope")
    ] = CurrentTaskScopeSelection()
    change: Annotated[
        TaskChange, *tool_field("Original declaration or correction naming its original reference")
    ] = OriginalTaskChange()


@dataclass(frozen=True, kw_only=True)
class CommsDecisionTool(AuthoredTaskRequest):
    label = "Record Decision"
    description = (
        "Record a chosen option and its rejected alternatives on the original "
        "message. Defaults to the current project/goal or turn; corrections name "
        "the original decision and grant no execution."
    )
    chosen: Annotated[str, *tool_field("Chosen option, preserving exact wording")]
    rejected: Annotated[
        tuple[str, ...], *tool_field("Nonempty unique rejected alternatives")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsConstraintTool(AuthoredTaskRequest):
    label = "Record Constraint"
    description = (
        "Record a restriction verbatim on the original message, under your own "
        "authorship rather than inferred human authority. Defaults to the current "
        "project/goal or turn; corrections name the original constraint."
    )
    text: Annotated[
        str, *tool_field("Exact restriction wording, never a paraphrase of another source")
    ]


@dataclass(frozen=True, kw_only=True)
class CommsSubtaskTool(AuthoredTaskRequest):
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


@dataclass(frozen=True, kw_only=True)
class CommsHistoryTool(ReadRequest[HistoryPageResult]):
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


@dataclass(frozen=True, kw_only=True)
class CommsRestartQueueTool(ReadRequest):
    label = "Restart Queue Status"
    description = "Inspect queued, restarted, stale, or uncertain attempts for a thread."
    name: Annotated[str, *tool_field("Agent thread")]


@dataclass(frozen=True, kw_only=True)
class CommsCancelRestartTool(ToolRequest):
    label = "Cancel Queued Restart"
    description = "Cancel pending restarts for a thread; cannot undo an attempted restart."
    name: Annotated[str, *tool_field("Agent thread")]


@dataclass(frozen=True, kw_only=True)
class CommsForkTool(ToolRequest):
    label = "Comms Fork"
    description = "Fork a child agent thread from a registered parent using its backend's fork semantics."
    name: Annotated[str, *tool_field("Child thread name")]
    parent: Annotated[str, *tool_field("Parent thread name", ContextSource(SubjectBinding))]
    task: Annotated[str, *tool_field("Optional task for the child")] = ""
    tags: Annotated[
        TagChoice, *tool_field("Comma-separated tags; omitted inherits parent tags")
    ] = InheritedTags()


@dataclass(frozen=True)
class InstallLaunches(ToolResult):
    """One line per configured main the install covers: launched, already running, or why not."""

    launches: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CommsInstallTool(ToolRequest[InstallLaunches]):
    label = "Install Latest Mains"
    description = (
        "Launch Core's installer for every configured main the installed build lacks: the same "
        "launch as `order install`. Returns once the install's unit is running; an install of "
        "the same target already running counts as this launch. The install stops and restarts "
        "teammates and the connector and posts its result to #comms."
    )


@dataclass(frozen=True, kw_only=True)
class CommsStopTool(ToolRequest):
    label = "Stop Comms Thread"
    description = "Stop a thread after verifying that its process owns the registered identity."
    name: Annotated[str, *tool_field("Thread name", ContextSource(SubjectBinding))]


@dataclass(frozen=True, kw_only=True)
class CommsCliInboxTool(ToolRequest):
    label = "Switch to CLI Inbox"
    description = (
        "Switch a native Pi thread onto a CLI inbox without changing its identity, "
        "aliases, tags, goal, orders, inbox cursor, or saved Pi session. Stops any "
        "running native Pi process first so an external CLI or MCP session can take over."
    )
    name: Annotated[str, *tool_field("Thread name", ContextSource(SubjectBinding))]


@dataclass(frozen=True, kw_only=True)
class CommsNativeThreadTool(ToolRequest):
    label = "Switch to Native Pi"
    description = (
        "Switch an independent CLI-inbox thread onto native Pi without changing its identity, "
        "aliases, tags, goal, orders, or inbox cursor. Reuses its preserved Pi session when "
        "present, or allocates a fresh session otherwise."
    )
    name: Annotated[str, *tool_field("Thread name", ContextSource(SubjectBinding))]


@dataclass(frozen=True, kw_only=True)
class CommsStartTool(ToolRequest):
    label = "Start Comms Thread"
    description = (
        "Start a stopped agent thread with its saved conversation and configuration. "
        "Reuses an already-running owner without interrupting it. The returned PID is "
        "reserved; startup may still be in progress. Does not send a new task."
    )
    name: Annotated[str, *tool_field("Thread to start", ContextSource(SubjectBinding))]


@dataclass(frozen=True, kw_only=True)
class CommsQueueRestartTool(ToolRequest):
    label = "Queue Restart When Idle"
    description = (
        "Queue a running thread to restart once it is idle. Never interrupts an active "
        "turn, never retries automatically if the process changed, and restarts in the "
        "current runtime without deploying a new build."
    )
    name: Annotated[str, *tool_field("Live agent thread", ContextSource(SubjectBinding))]


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(ToolRequest):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    name: Annotated[str, *tool_field("Stopped thread name", ContextSource(SubjectBinding))]
