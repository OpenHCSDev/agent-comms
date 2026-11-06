"""Named external tool requests; declarations own schema, context and behavior."""

from __future__ import annotations

import asyncio

from agent_comms_hooks import ToolRefusedError
from agent_comms.nominal.field_codec import WireExclude

from .input_key import InputKeyText
from .agent_inbox import InboxWait, NoWait, TakenInputPosition, TakenInputStart

from .tool_request import (
    HumanToolRequest,
    ObservedToolRequest,
    ListedNames,
    LiveOwnerTool,
    OmittedNames,
    PublishingTool,
    PublishingToolRequest,
    ReadTool,
    SubjectBinding,
    ThreadReadTool,
    ThreadContextAction,
    ThreadToolRequest,
    CoreRead,
    ToolRequest,
    NativeToolRequest,
    tool_field,
)
from .tool_declaration import NativeRequestTools
from .nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from .order_tools import MessagePublished
from .native_tools import CommunicationCall

import dataclasses
import threading
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Type

from pydantic import JsonValue

from agent_comms.lifecycle_capability import Active

from .change_report import NoChangeReport, ReportEvidence
from .channel_management import ListTagEdit, TagEdit
from agent_comms.channel_targets import ChannelSelection, ChannelTarget, CommandTarget, NamedTarget, NoChannel
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
from .config_options import ConfigOptions
from .cli_command import PreparedTargetAction, TargetAction, TargetActionCommand, TargetEdit
from .cli_result import CliResult, TargetEditResult
from .coordination.tables.refusals import SendRefusal
from .display_order import ChannelSort, ThreadSort
from .nominal.field_codec import FieldCodec, NullMember, SingleFieldValue, SubclassUnion, WireForm, WireName, WireOmitDefault, projected, GoalId
from .goal_actions import (
    ActiveGoalAction, ClearGoalAction, EditGoalAction, GoalAction, GoalActor, GoalPrecondition,
    ModelInvocable, PausedGoalAction, PeerInvocable, RuntimeInvocable, SetGoalAction,
    TransitionGoalAction,
)
from .goal_history import RecordedGoalHistory
from .goal_scope import AllGoals, OneGoal
from .goal_presentation import GoalExecution, GoalSnapshot
from .goal_waits import NoStandbyReview, StandbyReview
from .goals import GoalDeclaration
from .history_result import HistoryPageResult
from .input_attempt import UnresolvedInput
from .message_page import NEWEST_PAGE, DisplayedMessagePage, InboxPage, MessagePage, PageBounds
from .page_traversal import LatestSequence, LiveCursor
from .historical_views import ConversationObservation, ConversationRevision, UnobservedConversation
from .message_reference import MessageReference
from .messages import InfoKind, Message, MessageKind, NoticeRole, SendableMessageKind
from .owner_lifecycle import OwnerStartResult
from .owner_launch import HostResources
from .page_traversal import NewestCursor, PageCursor
from .pi_payloads import PublishedGoalToolDetails
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .notification_evidence import AfterRecipient, FirstRecipient, RecipientCursor
from .message_notification import MessageNotification
from .presentation import ChannelView, WireRevision
from .runtime_requests import QueueActionRuntimeRequest
from .runtime_server import RuntimeRequest
from .owner_connection import OwnerConnection
from .context_store import (
    LATEST_CONTEXT, CapturedContext, ContextManifestCursor, ContextManifestPage, ContextManifestReference,
)
from .turn_context import ContextManifest
from .importing import ImportedSessionMetadata, ThreadImported
from .exporting import WireExportFormat, WireExportLimit, WireExportProvenance, WireExportScope
from .historical_views import AllHistoricalSenders, HistoricalSenders, HistoricalThread
from .cli_bridges.history import ExpectedSession, OfflineHistory
from .harness_install import Harness
from .thread_management import ImportDirectory
from .registry_document import RegistryEntry
from .session_revision import SessionObservation
from .turn_context import CodexRolloutProvenance, ContextSourceText, Provenance, SegmentManifest
from .working_memory_annotations import WorkingMemoryAnnotations
from .working_memory_labels import JevClassifier, ModelLabel
from .nominal.field_codec import ThreadLookup
from .notification_evidence import RecipientLimit
from .progress_claims import ProgressClaim
from .relationships import (
    Collaboration,
    GoalDerivedContact,
    GoalMentionDiagnostic,
    RelationshipEdit,
    SortableRelationshipGroup,
    ThreadCommsSnapshot,
)
from .restart_queue import QueuedRestart, RestartQueued, RestartQueue
from .team.standing import InstallDue
from .send_result import CorrectionComplete, CorrectionResult, SendResult
from .repo import Checkout
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
from .threads import (
    ArchiveOwnerControl,
    NativeOwnerCommand,
    OwnerLifecycleControl,
    StartOwnerControl,
    NextTurnConfiguration,
    Thread,
)
from .thread_management import (
    InheritedTags,
    ProjectChangeResult,
    RenameThreadResult,
    TagChoice,
    ThreadTitleResult,
)
from .input_origin import HumanInputOrigin
from .thread_identity import ClaimedIdentity
from .thread_presentation import ThreadPresentation
from .child_process import ProcessIdentity
from .thread_identity import ToolName
from .thread_model import ThreadModel
from .tool_caller import ThreadCaller, ThreadListing, ToolCaller
from .acp_extension import CoordinationChangedUpdate
from .history_views import RetiredViews
from .thread_identity import ThreadIncarnation
from .display_references import ReferenceNames
from .registry_document import ProjectThreadSelection, RegistrySlice, RegistrySnapshot
from .transcripts import TranscriptCursor
from .host_logs import HostLog, HostLogRead
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

    @projected(view=WireExclude, name="agentCommsKind")
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
class TeammateGoalChanged(ToolResult):
    """A teammate's goal as this caller left it. It is never the caller's own publication, so
    the caller's turn permission does not take it."""

    thread: str
    goal: GoalDeclaration


@dataclass(frozen=True, slots=True)
class OwnGoal(NullMember):
    """No thread was named: the caller's own goal."""

    actor: ClassVar[type[GoalActor]] = RuntimeInvocable

    def thread_name(self, caller: ThreadCaller) -> str:
        return caller.name

    def published(self, own: ToolResult, goal: GoalDeclaration) -> ToolResult:
        return own


@dataclass(frozen=True, slots=True)
class TeammateGoal(SingleFieldValue):
    """A teammate's goal, changed under PeerInvocable's consent rule."""

    thread: str
    actor: ClassVar[type[GoalActor]] = PeerInvocable

    def thread_name(self, caller: ThreadCaller) -> str:
        return self.thread

    def published(self, own: ToolResult, goal: GoalDeclaration) -> ToolResult:
        return TeammateGoalChanged(self.thread, goal)


@dataclass(frozen=True, kw_only=True)
class GoalChangeTool(ThreadToolRequest):
    """Your own goal, or a teammate's when thread names one: responsibility for goals is
    shared, and PeerInvocable owns what a teammate may not change."""

    thread: Annotated[
        OwnGoal | TeammateGoal, *tool_field("Teammate whose goal to change; omit for your own")
    ] = OwnGoal()

    def update(self, comms: Comms, caller: ThreadCaller, action: GoalAction) -> GoalDeclaration:
        return comms.goals.update_goal(
            self.thread.thread_name(caller), action, actor=self.thread.actor)

    def current(self, comms: Comms, caller: ThreadCaller, goal_id: GoalId):
        return comms.registry.require(self.thread.thread_name(caller)).goal.require().require_id(goal_id)


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
    """One page of messages nothing took yet; reading it records nothing. A reader on the
    other side of the connector decodes it, so its messages are declared as messages, like a
    MessagePage's."""

    messages: tuple[Message, ...]
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
    def selected(self, catalog: tuple[type[NativeRequestTools], ...]) -> tuple[type[NativeRequestTools], ...]: ...


@dataclass(frozen=True)
class NamedToolLookup(ToolLookup, declared_name="named"):
    tools: Annotated[tuple[ToolName, ...], WireName("names")]

    def selected(self, catalog: tuple[type[NativeRequestTools], ...]) -> tuple[type[NativeRequestTools], ...]:
        members = {ToolName(tool.declared_name): tool for tool in catalog}
        unknown = set(self.tools) - members.keys()
        if unknown:
            raise UnknownFamilyMember(f"Unknown native tools: {', '.join(sorted(name.value for name in unknown))}")
        return tuple(members[name] for name in dict.fromkeys(self.tools))


@dataclass(frozen=True)
class SearchToolLookup(ToolLookup, declared_name="search"):
    query: str

    def selected(self, catalog: tuple[type[NativeRequestTools], ...]) -> tuple[type[NativeRequestTools], ...]:
        terms = self.query.casefold().split()
        return tuple(tool for tool in catalog
                     if all(term in f"{tool.declared_name} {tool.label} {tool.description}".casefold()
                            for term in terms))


@dataclass(frozen=True, kw_only=True)
class CommsDiscoverTool(ReadTool, NativeToolRequest[ToolCaller, ToolsSelected]):
    """The native adapter activates these declarations through Pi's existing tool loader."""

    label = "Find and load Comms tools"
    description = (
        "Find and load any Comms operation before calling it. Search by words in its name "
        "or description, or select exact names. An explicitly empty search loads every tool. "
        "Tools stay active for this turn, not for the session's lifetime. "
        "Use this for messages, inbox, goals, orders, reviews, threads, channels and configuration."
    )
    loads_tools = True
    native_call = CommunicationCall
    selection: Annotated[ToolLookup, *tool_field("Search words or select exact tool names")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ToolsSelected:
        return ToolsSelected(tuple(AvailableTool(ToolName(tool.declared_name), tool.label, tool.description)
                                   for tool in self.selection.selected(NativeRequestTools.members_with(NativeRequestTools))))


@dataclass(frozen=True, kw_only=True)
class CommsChannelTool(CoreRead[Channel]):
    """One channel as Core declares it; a UI asks it whether the channel takes input."""

    label = "Channel"
    description = "One channel's declaration: its kind, members and whether it is read-only."
    name: Annotated[str, *tool_field("Channel name")]

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return comms.bus.require_channel(self.name)



@dataclass(frozen=True)
class HistoricalThreadsRead(ToolResult):
    threads: tuple[HistoricalThread, ...]


@dataclass(frozen=True, kw_only=True)
class CommsHistoricalThreadsTool(CoreRead[HistoricalThreadsRead]):
    """The preserved identities a UI's saved-sessions view lists, for every sender or one."""

    label = "Historical threads"
    description = "Preserved thread identities from attached history sources, for every sender or one name."
    senders: Annotated[HistoricalSenders, *tool_field("Every sender, or one name")] = AllHistoricalSenders()

    def apply(self, comms: Comms, caller: ToolCaller) -> HistoricalThreadsRead:
        return HistoricalThreadsRead(comms.bus.history.threads(self.senders))


@dataclass(frozen=True, kw_only=True)
class CommsExportWireTool(
    ObservedToolRequest[ToolCaller, WireExportProvenance], HumanToolRequest[WireExportProvenance],
):
    """A UI exports wire history to a file on Core's host, with progress and cancellation.
    Requires a human caller."""

    label = "Export wire"
    description = ("Export wire history to a file on Core's host, reporting progress and stopping "
                   "when cancelled; requires a human UI caller.")
    destination: Annotated[Path, *tool_field("Destination file on Core's host")]
    format: Annotated[WireExportFormat, *tool_field("Export format")]
    scope: Annotated[WireExportScope, *tool_field("What to export")]
    limit: Annotated[WireExportLimit, *tool_field("How much to export")]

    def answered(self, comms: Comms) -> WireExportProvenance:
        return comms.views.export_wire(self.destination, format=self.format, scope=self.scope,
                                       limit=self.limit, observer=self.observer)


@dataclass(frozen=True, kw_only=True)
class CommsImportThreadTool(HumanToolRequest[ThreadImported]):
    """A UI imports a harness session as a stopped thread; Core's host reads the source file.
    Requires a human caller."""

    label = "Import thread"
    description = ("Import a harness session file on Core's host as a stopped thread; requires a "
                   "human UI caller.")
    source: Annotated[Path, *tool_field("Session file on Core's host")]
    harness: Annotated[type[Harness], *tool_field("Harness that wrote the session")]
    name: Annotated[str, *tool_field("Thread name")]
    session: Annotated[ExpectedSession, *tool_field("Expected session")]
    worktree: Annotated[ImportDirectory, *tool_field("Worktree for the imported thread")]

    def answered(self, comms: Comms) -> ThreadImported:
        return comms.threads.import_thread(OfflineHistory.stored(self.harness, self.source, self.session),
                                          name=self.name, worktree=self.worktree)


@dataclass(frozen=True)
class ContextInspectionRead(ToolResult):
    entry: RegistryEntry
    page: ContextManifestPage
    source: SessionObservation
    annotations: tuple[ModelLabel, ...]
    imported: tuple[CodexRolloutProvenance, ...]


@dataclass(frozen=True, kw_only=True)
class CommsContextInspectionTool(CoreRead[ContextInspectionRead]):
    """One thread's recorded context page, with its annotations and imported sources, as a
    context explorer shows it."""

    label = "Context inspection"
    description = "One thread's recorded context manifests, working-memory annotations and imported sources."
    owner: Annotated[str, *tool_field("Thread whose context to read")]
    cursor: Annotated[ContextManifestCursor, *tool_field("Where the page starts")] = LATEST_CONTEXT
    previous: Annotated[tuple[CapturedContext, ...], *tool_field("Contexts the reader already holds")] = ()

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextInspectionRead:
        entry = comms.registry.entry(self.owner).entry
        thread = entry.thread
        page = comms.contexts.page(self.owner, comms.registry, cursor=self.cursor, previous=self.previous)
        annotations = WorkingMemoryAnnotations.for_context(
            comms.registry.store.coordinator_reader,
            tuple(resource.value for resource in page.resources), JevClassifier.version())
        return ContextInspectionRead(
            entry, page, thread.runtime.session_observation(comms.root, thread), annotations,
            ImportedSessionMetadata.sources_for_owner(comms.registry, thread))


@dataclass(frozen=True, kw_only=True)
class CommsImportedSourceTool(CoreRead[ContextSourceText]):
    """The public text of one source an imported thread's snapshot retained."""

    label = "Imported source"
    description = "The public text of one source in an imported thread's original snapshot."
    owner: Annotated[Thread, *tool_field("The imported thread as the reader saw it")]
    source: Annotated[Provenance, *tool_field("The retained source")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextSourceText:
        if self.source not in ImportedSessionMetadata.sources_for_owner(comms.registry, self.owner):
            raise ValueError("Source is outside the original imported snapshot")
        return ContextSourceText(self.source.public_description(), self.source.public_text(comms))


@dataclass(frozen=True, kw_only=True)
class SavedManifestRead[R: ToolResult = ToolResult](CoreRead[R], declared_name=None):
    """A read of one retained context manifest of a stopped thread."""

    owner: Annotated[str, *tool_field("Thread whose retained manifest to read")]
    reference: Annotated[ContextManifestReference, *tool_field("The retained manifest")]
    position: Annotated[int, *tool_field("Segment position")]

    def manifest(self, comms: Comms) -> ContextManifest:
        return self.reference.read(comms.contexts, self.owner, comms.registry)


@dataclass(frozen=True, kw_only=True)
class CommsSavedSourceTool(SavedManifestRead[ContextSourceText]):
    """The public text of one source in a stopped thread's retained manifest."""

    label = "Saved context source"
    description = "The public text of one source in a stopped thread's retained context manifest."
    source: Annotated[Provenance, *tool_field("The retained source")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextSourceText:
        return self.manifest(comms).public_source_text(comms, self.position, self.source)


@dataclass(frozen=True, kw_only=True)
class CommsSavedSegmentTool(SavedManifestRead[SegmentManifest]):
    """One segment of a stopped thread's retained manifest, with its selected contributors."""

    label = "Saved context segment"
    description = "One segment of a stopped thread's retained context manifest."
    contributors: Annotated[tuple[int, ...], *tool_field("Contributor positions")] = ()

    def apply(self, comms: Comms, caller: ToolCaller) -> SegmentManifest:
        return self.manifest(comms).selected_segment(self.position, self.contributors)


@dataclass(frozen=True)
class OwnerAnswered(ToolResult):
    payload: JsonValue


@dataclass(frozen=True, kw_only=True)
class CommsOwnerRequestTool(HumanToolRequest[OwnerAnswered]):
    """A UI asks one running owner incarnation a declared runtime request, such as its
    context; Core's host reaches the owner's socket. Requires a human caller."""

    label = "Owner request"
    description = ("Send a declared runtime request to one running thread incarnation's owner "
                   "and return its reply; requires a human UI caller.")
    owner: Annotated[ThreadIncarnation, *tool_field("The owner incarnation the UI holds")]
    request: Annotated[RuntimeRequest, *tool_field("The declared runtime request")]

    def answered(self, comms: Comms) -> OwnerAnswered:
        thread = comms.registry.require(self.owner.name)
        if thread.incarnation != self.owner:
            raise ValueError("Selected thread incarnation changed")
        return OwnerAnswered(FieldCodec.decode(JsonValue, OwnerConnection.exchange_at(
            comms.root, thread, self.request)))


@dataclass(frozen=True, kw_only=True)
class CommsMoveThreadProjectTool(HumanToolRequest[ProjectChangeResult]):
    """The owner's UI moves one thread incarnation's project; a later occupant of the name
    is refused, never moved."""

    label = "Move thread project"
    description = "Move a thread incarnation's project directory; requires a human UI caller."
    owner: Annotated[ThreadIncarnation, *tool_field("The thread incarnation the UI holds")]
    path: Annotated[str, *tool_field("New project directory")]

    def answered(self, comms: Comms) -> ProjectChangeResult:
        return comms.threads.set_project(self.owner, self.path)


@dataclass(frozen=True, kw_only=True)
class CommsRenameManagedThreadTool(HumanToolRequest[RenameThreadResult]):
    """The owner's UI renames the running thread its agent process manages."""

    label = "Rename managed thread"
    description = "Rename a running managed thread after proving its process; requires a human UI caller."
    thread: Annotated[ThreadIncarnation, *tool_field("The thread incarnation the UI holds")]
    display_name: Annotated[str, *tool_field("The new display name")]
    owner: Annotated[ProcessIdentity, *tool_field("The process that manages the thread")]

    def answered(self, comms: Comms) -> RenameThreadResult:
        return comms.threads.rename_managed_thread(self.thread, self.display_name, owner=self.owner)


@dataclass(frozen=True, kw_only=True)
class CommsSetThreadTitleTool(HumanToolRequest[ThreadTitleResult]):
    """The owner's UI corrects a managed thread's label without renaming it."""

    label = "Set managed thread title"
    description = (
        "Correct a managed thread's display title while its name and routing aliases stay"
        " unchanged; requires a human UI caller."
    )
    thread: Annotated[ThreadIncarnation, *tool_field("The thread incarnation the UI holds")]
    title: Annotated[str, *tool_field("The new display title")]
    owner: Annotated[ProcessIdentity, *tool_field("The process that manages the thread")]

    def answered(self, comms: Comms) -> ThreadTitleResult:
        return comms.threads.set_managed_title(self.thread, self.title, owner=self.owner)


@dataclass(frozen=True, kw_only=True)
class CommsCaptureHumanInputTool(HumanToolRequest[HumanInputOrigin]):
    """The original human author witness for input the owner is about to queue, captured
    against the claimed identity on Core's host."""

    label = "Capture human input"
    description = "Capture the human author witness for queued input; requires a human UI caller."
    claimed_identity: Annotated[ClaimedIdentity, *tool_field("The claimed input identity")]

    def answered(self, comms: Comms) -> HumanInputOrigin:
        return HumanInputOrigin.capture(comms, self.claimed_identity)


@dataclass(frozen=True, kw_only=True)
class CommsThreadPresentationTool(CoreRead[ThreadPresentation]):
    """One thread's published presentation, as the sidebar and session header show it."""

    label = "Thread presentation"
    description = "Read one thread's published presentation."
    thread: Annotated[str, *tool_field("Thread name")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadPresentation:
        return comms.views.thread_presentations((self.thread,)).read(self.thread).published()


@dataclass(frozen=True)
class QueueActionApplied(ToolResult):
    owner: str


@dataclass(frozen=True, kw_only=True)
class CommsQueueActionTool(HumanToolRequest[QueueActionApplied]):
    """A UI sends a queued message now, or clears a queue, at its original recipient's owner;
    Core's host reaches the owner. Requires a human caller."""

    label = "Queue action"
    description = ("Send a queued message now, or clear the queue, at the original recipient's "
                   "running owner; requires a human UI caller.")
    lookup: Annotated[ThreadLookup, *tool_field("The original recipient's creation lookup")]
    action: Annotated[type[QueueActionRuntimeRequest], *tool_field("Send now or clear")]

    def answered(self, comms: Comms) -> QueueActionApplied:
        original = comms.registry.snapshot().born(self.lookup)
        self.action.at_owner(comms.root, original)
        return QueueActionApplied(original.name)



@dataclass(frozen=True, kw_only=True)
class CommsWireRevisionTool(CoreRead[WireRevision]):
    """What every wire view derives from; an equal revision shows the same thing."""

    label = "Wire revision"
    description = "The wire revision thread, relationship and conversation views derive from."

    def apply(self, comms: Comms, caller: ToolCaller) -> WireRevision:
        return comms.views.revision()


@dataclass(frozen=True)
class PageMarked(ToolResult):
    messages: int


@dataclass(frozen=True, kw_only=True)
class CommsMarkPageReadTool(HumanToolRequest[PageMarked]):
    """A UI acknowledges exactly the page it painted, as the human user of its project."""

    label = "Mark page read"
    description = ("Acknowledge the read evidence of exactly one painted conversation page, as "
                   "the project's human user; requires a human UI caller.")
    page: Annotated[MessagePage, *tool_field("The painted page")]
    worktree: Annotated[Checkout, *tool_field("The project whose human user read it")]

    def answered(self, comms: Comms) -> PageMarked:
        comms.views.mark_page_read(self.page, worktree=self.worktree)
        return PageMarked(len(self.page.messages))



@dataclass(frozen=True, kw_only=True)
class CommsThreadRelationshipsTool(CoreRead[ThreadCommsSnapshot]):
    """One thread's relationship groups, as its sidebar shows them."""

    label = "Thread relationships"
    description = "One thread's inbound, outbound, parent, child and collaborating threads."
    owner: Annotated[str, *tool_field("Thread whose relationships to read")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadCommsSnapshot:
        return comms.relationships.snapshot(self.owner)


@dataclass(frozen=True)
class RelationshipOrdered(ToolResult):
    order: type[ThreadSort]


@dataclass(frozen=True, kw_only=True)
class CommsSetRelationshipOrderTool(HumanToolRequest[RelationshipOrdered]):
    """A UI sets how one of a thread's relationship groups is sorted; requires a human caller."""

    label = "Set relationship order"
    description = "Set how one sortable relationship group of a thread is ordered; requires a human UI caller."
    owner: Annotated[str, *tool_field("Thread whose group to order")]
    group: Annotated[type[SortableRelationshipGroup], *tool_field("Relationship group")]
    order: Annotated[type[ThreadSort], *tool_field("Thread order")]

    def answered(self, comms: Comms) -> RelationshipOrdered:
        return RelationshipOrdered(comms.relationships.set_order(self.owner, self.group, self.order))


@dataclass(frozen=True)
class TargetActionsRead(ToolResult):
    actions: tuple[TargetAction, ...]


@dataclass(frozen=True, kw_only=True)
class CommsTargetActionsTool(CoreRead[TargetActionsRead]):
    """A UI's thread or channel menu: the actions its selection offers. Opening one prepares
    its fields through CommsPrepareTargetActionTool, so listing never runs Pi."""

    label = "Target actions"
    description = "The actions a thread or channel menu offers for these targets."
    project: Annotated[Checkout, *tool_field("Project the menu was opened in")]
    targets: Annotated[tuple[CommandTarget, ...], *tool_field("Selected threads or channels")]
    channel: Annotated[ChannelSelection, *tool_field("Surrounding channel")] = NoChannel()

    def apply(self, comms: Comms, caller: ToolCaller) -> TargetActionsRead:
        return TargetActionsRead(TargetActionCommand.target_catalog(
            comms, TargetActionCommand.selected_targets(self.targets), self.channel, project=self.project))


@dataclass(frozen=True, kw_only=True)
class CommsPrepareTargetActionTool(CoreRead[PreparedTargetAction]):
    """Opening a menu action, or changing its values, asks Core which choices it offers."""

    label = "Prepare target action"
    description = ("The choices Core's configuration offers a menu action's fields, such as the "
                   "thinking levels of its chosen model.")
    action: Annotated[TargetAction, *tool_field("The action with its current values")]

    def apply(self, comms: Comms, caller: ToolCaller) -> PreparedTargetAction:
        return asyncio.run(self.action.prepared(ConfigOptions.for_route(comms.route)))


@dataclass(frozen=True, kw_only=True)
class CommsTargetEditTool(HumanToolRequest[TargetEditResult | Channel]):
    """A UI applies one menu action, as the owner, locally or across the connector."""

    label = "Apply target action"
    description = ("Apply a thread or channel menu action with the values its editor accepted; "
                   "requires a human UI caller.")
    edit: Annotated[TargetEdit, *tool_field("The accepted action and its values")]

    def answered(self, comms: Comms) -> TargetEditResult | Channel:
        return self.edit.apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsRetiredViewsTool(CoreRead[RetiredViews]):
    """A UI asks which of its open thread and channel views lost their source."""

    label = "Retired views"
    description = "Which of these thread incarnations and channel views were deleted, replaced or archived."
    threads: Annotated[frozenset[ThreadIncarnation], *tool_field("Open thread views")]
    channels: Annotated[frozenset[str], *tool_field("Open channel views")]

    def apply(self, comms: Comms, caller: ToolCaller) -> RetiredViews:
        return comms.views.retired_views(self.threads, self.channels)



@dataclass(frozen=True, kw_only=True)
class CommsThreadConfigurationTool(CoreRead[CoordinationChangedUpdate]):
    """A UI shows a thread's configuration from Core, in process or across the connector."""

    label = "Thread configuration"
    description = "One thread's configuration: its model, thinking level, title and worktree."
    thread: Annotated[str, *tool_field("Thread name")]

    def apply(self, comms: Comms, caller: ToolCaller) -> CoordinationChangedUpdate:
        return comms.configuration.answer(self.thread)



@dataclass(frozen=True, kw_only=True)
class CommsSessionThreadTool(CoreRead[ProjectThreadSelection]):
    """An unbound UI session finds its project's thread; Core's host resolves the path its
    registry records, so a detached copy never matches through a local alias."""

    label = "Session thread"
    description = ("Select the thread an unbound session at a project names: its preferred name, "
                   "else the project's sole declared ACP thread.")
    project: Annotated[str, *tool_field("Project path as the UI names it")]
    preferred: Annotated[str, *tool_field("Preferred thread name")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ProjectThreadSelection:
        return comms.registry.session_thread(Path(self.project), self.preferred)


@dataclass(frozen=True, kw_only=True)
class CommsPinChannelTool(NativeToolRequest):
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
class CommsPinThreadTool(NativeToolRequest):
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
class CommsModelTool(NextTurnConfiguration, NativeOwnerCommand, NativeToolRequest):
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
        thread = comms.threads.set_thread_configuration(original, self)
        return ModelChanged(thread.name, thread.model, thread.thinking_level, thread.service_tier)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelsTool(NativeToolRequest[ToolCaller, ChannelOrderSet]):
    label = "Sort Channels"
    description = (
        "Persist channel-list ordering independently of member ordering. Pinned "
        "channels stay first, with built-in channels first within each pin group."
    )
    order: Annotated[type[ChannelSort], *tool_field("Channel list order")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelOrderSet:
        return ChannelOrderSet(comms.channels.set_channel_order(self.order))


@dataclass(frozen=True, kw_only=True)
class CommsTagsTool(NativeToolRequest):
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
class CommsThreadTagsTool(NativeToolRequest):
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
class CommsChannelsTool(ReadTool, NativeToolRequest):
    label = "List channels"
    description = "List named tag views and their current member threads; #any includes everyone."

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        catalog = comms.channels.catalog.read()
        return ChannelListing(comms.views.channel_views(), tuple(catalog.saved_views.values()),
                              catalog.list_order)


@dataclass(frozen=True, kw_only=True)
class CommsConversationPageTool(CoreRead[MessagePage]):
    """The UI's conversation read: the same request locally and through the connector."""

    label = "Conversation page"
    description = "Read one bounded display page of a channel or a DM as the owner's UI shows it."
    conversation: Annotated[NamedTarget, *tool_field("The channel or the DM's thread")]
    project: Annotated[Checkout, *tool_field("The project whose human user reads the page")]
    bounds: Annotated[PageBounds, *tool_field("Which page: direction, size; default newest")] = (
        NEWEST_PAGE
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> MessagePage:
        history = self.conversation.display_history(comms.views, self.project)
        return history.page(comms.views.presentation, self.bounds)


@dataclass(frozen=True, kw_only=True)
class CommsConversationRevisionTool(CoreRead[ConversationRevision]):
    """The UI's change check for one conversation: the same request locally and through the
    connector. The UI keeps the revision it gets back and sends it with the next check."""

    label = "Conversation revision"
    description = (
        "Check a channel or a DM for changes since the UI's last observation; returns the "
        "revision to compare and keep."
    )
    conversation: Annotated[NamedTarget, *tool_field("The channel or the DM's thread")]
    project: Annotated[Checkout, *tool_field("The project whose human user reads it")]
    seen: Annotated[
        ConversationObservation, *tool_field("What the UI last saw; unobserved the first time")
    ] = UnobservedConversation()

    def apply(self, comms: Comms, caller: ToolCaller) -> ConversationRevision:
        history = self.conversation.display_history(comms.views, self.project)
        return history.revision(comms.views, self.conversation, self.seen, worktree=self.project)


@dataclass(frozen=True, kw_only=True)
class CommsLatestSequenceTool(CoreRead[LatestSequence]):
    """The bound a UI reads new messages up to, locally or through the connector."""

    label = "Latest sequence"
    description = "The sequence number of the newest message on the bus, whoever it was sent to."

    def apply(self, comms: Comms, caller: ToolCaller) -> LatestSequence:
        return LatestSequence(LiveCursor(comms.views.latest_sequence()))


@dataclass(frozen=True)
class ServedRoot(ToolResult):
    """The bus root a Core serves: the key its observation requests and views are read under."""

    root: str


@dataclass(frozen=True, kw_only=True)
class CommsServedRootTool(CoreRead[ServedRoot]):
    """A remote UI keys its observation by the root the connector's Core serves."""

    label = "Served root"
    description = "The bus root this Core serves; a UI keys its observation requests by it."

    def apply(self, comms: Comms, caller: ToolCaller) -> ServedRoot:
        return ServedRoot(str(comms.root.resolve()))


@dataclass(frozen=True, kw_only=True)
class CommsSetChannelMetadataTool(NativeToolRequest):
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
class CommsSetViewTool(NativeToolRequest):
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
class CommsDeleteViewTool(NativeToolRequest):
    label = "Delete non-routable view"
    description = "Delete a saved projection without changing tags, channels, messages, or threads."
    name: Annotated[str, *tool_field("Saved view name")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        DeleteViewCliCommand(name=self.name).apply(comms)
        return CommsChannelsTool().apply(comms, caller)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelTool(NativeToolRequest[ToolCaller, Channel]):
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
        result = comms.threads.set_project(caller.identity(comms), self.path)
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
class CommsSetGoalTool(GoalChangeTool):
    label = "Set persistent goal"
    description = (
        "Set or replace your own persistent goal, or a teammate's when thread names one. "
        "Use this when the user asks you to start, set, or pursue a goal autonomously."
    )
    text: Annotated[str, *tool_field("Persistent objective to pursue")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        goal = self.update(comms, caller, SetGoalAction(text=self.text))
        return self.thread.published(GoalSet(
            goal, "Persistent goal activated. Work toward it and report progress with comms_goal."),
            goal)


@dataclass(frozen=True, kw_only=True)
class CommsGoalTool(ThreadToolRequest):
    label = "Goal progress"
    description = (
        "Update goal progress; complete it or block it when user input is needed. Use "
        "standby with explicit wait_for thread names when no independent work remains "
        "and a named collaborator must supply the next result. "
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
            "Typed evidence claims, each with the artifact Core verifies; "
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
class CommsResumeGoalTool(GoalChangeTool):
    label = "Resume paused goal"
    description = (
        "Resume a paused goal, your own or a teammate's, with the same ID only after an explicit "
        "request. This does not replace the goal or resume blocked/completed goals."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the paused goal to resume")]
    progress: Annotated[str, *tool_field("Progress summary for the resumed goal")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        goal_id = self.goal_id
        current = self.current(comms, caller, goal_id)
        current.state.require_model_resume()
        progress = self.progress
        goal = self.update(
            comms, caller,
            ActiveGoalAction(
                expect=GoalPrecondition(
                    expected_checkpoint=current.captured,
                    goal_id=OneGoal(goal_id),
                ),
                progress=progress,
            ),
        )
        goal = goal.require()
        if not goal.state.items_with(Active, goal) or goal.progress != progress:
            raise ToolRefusedError("Goal changed during resume; refresh its state.")
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsEditGoalTool(GoalChangeTool):
    label = "Edit goal text"
    description = (
        "Edit an existing goal's text, your own or a teammate's, while keeping its identity, "
        "status, and progress. Use comms_set_goal only to replace the objective with a fresh "
        "goal ID."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the existing goal to edit")]
    text: Annotated[str, *tool_field("Revised objective text, including any @mentions")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        goal_id = self.goal_id
        current = self.current(comms, caller, goal_id)
        goal = self.update(
            comms, caller,
            EditGoalAction(
                expect=GoalPrecondition(expected_checkpoint=current.captured, goal_id=OneGoal(goal_id)), text=self.text
            ),
        )
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsClearGoalTool(GoalChangeTool):
    label = "Clear goal"
    description = (
        "Clear a goal, your own or a teammate's, leaving that thread with no goal. Held "
        "problems must be finished or released first; a goal Tristan paused, or the chokehold "
        "holds, cannot be cleared by a teammate."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the goal to clear")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        current = self.current(comms, caller, self.goal_id)
        goal = self.update(
            comms, caller,
            ClearGoalAction(expect=GoalPrecondition(
                expected_checkpoint=current.captured, goal_id=OneGoal(self.goal_id))),
        )
        return self.thread.published(GoalUpdated(goal), goal)


@dataclass(frozen=True, kw_only=True)
class CommsPauseGoalTool(GoalChangeTool):
    label = "Pause goal"
    description = (
        "Pause a goal, your own or a teammate's, keeping its identity; comms_resume_goal "
        "resumes it. Pausing an already-paused goal keeps its original pause."
    )
    goal_id: Annotated[GoalId, *tool_field("Identity of the goal to pause")]
    progress: Annotated[str, *tool_field("Why the goal is paused")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        current = self.current(comms, caller, self.goal_id)
        goal = self.update(
            comms, caller,
            PausedGoalAction(
                expect=GoalPrecondition(expected_checkpoint=current.captured, goal_id=OneGoal(self.goal_id)),
                progress=self.progress,
            ),
        )
        return self.thread.published(GoalUpdated(goal), goal)


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
class CommsThreadsTool(ReadTool, NativeToolRequest):
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



@dataclass(frozen=True, kw_only=True)
class CommsRegistryTool(CoreRead[RegistrySnapshot]):
    """A remote UI resolves names, renames and recorded threads against Core's registry."""

    label = "Thread registry"
    description = "The thread registry snapshot: names, aliases, statuses and owner generations."

    def apply(self, comms: Comms, caller: ToolCaller) -> RegistrySnapshot:
        return comms.registry.snapshot()


@dataclass(frozen=True, kw_only=True)
class CommsRegistrySliceTool(CoreRead[RegistrySlice]):
    """A remote UI reads the registry facts of the threads it is showing, not every thread's."""

    label = "Thread registry slice"
    description = ("The thread registry snapshot holding only the named threads: their rows, "
                   "statuses, aliases and owner generations, with the fields every thread shares.")
    threads: Annotated[tuple[str, ...], *tool_field("Thread names, current or renamed")]

    def apply(self, comms: Comms, caller: ToolCaller) -> RegistrySlice:
        return comms.registry.snapshot().only(self.threads)


@dataclass(frozen=True, kw_only=True)
class CommsReferenceNamesTool(CoreRead[ReferenceNames]):
    """A UI resolves @thread and #channel links without the registry or channel activity."""

    label = "Reference names"
    description = "Every linkable thread name with its incarnation, and every channel name, without thread records."

    def apply(self, comms: Comms, caller: ToolCaller) -> ReferenceNames:
        return ReferenceNames.of(comms.registry.snapshot(), comms.channels.catalog.read().channel_names())


@dataclass(frozen=True)
class MessageOutcomes(ToolResult):
    """One message once, then each recipient outcome its caller may read."""

    message: Annotated[Message, WireForm]
    outcomes: tuple[MessageNotification, ...]
    remaining: int
    next: RecipientCursor


@dataclass(frozen=True, kw_only=True)
class CommsMessageOutcomesTool(ReadTool, NativeToolRequest):
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

    def apply(self, comms: Comms, caller: ToolCaller) -> MessageOutcomes:
        source, = comms.bus.bus.deliveries_for_references((self.reference,))
        if not caller.outcome_viewer(comms.registry).sees(source):
            raise ToolRefusedError(f"Message {source.message.seq} is not readable by this caller.")
        window = LiveNotificationRead(
            (self.reference,), after=self.after, limit=self.limit).apply(comms, caller)
        page = window.outcomes.get((source.message.seq, source.message.message_id), ())
        return MessageOutcomes(
            source.message, page, window.remaining(self.reference),
            AfterRecipient(page[-1].recipient_identity) if page else self.after,
        )


@dataclass(frozen=True, kw_only=True)
class CommsResourcesTool(ThreadReadTool):
    label = "Host resources"
    description = (
        "Read a running thread's shared Python host memory, module and owner counts, "
        "and cumulative registry read/capture/decode counts. Native model children "
        "are separate from this shared host measurement."
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> ToolResult:
        return HostResources.read(comms.root, comms.registry.require(caller.subject(self.thread)))


@dataclass(frozen=True, kw_only=True)
class CommsHostLogTool(ReadTool, NativeToolRequest):
    label = "Host log"
    description = (
        "Read the host's connector journal, latest install diagnostics or busiest processes, with "
        "secrets hidden: a session on another machine reads a failure itself instead of asking "
        "someone on the host."
    )
    longest: ClassVar[int] = 500

    source: Annotated[type[HostLog], *tool_field("What to read")]
    lines: Annotated[int, *tool_field("How many lines, newest last; from 1 to 500")] = 100

    def apply(self, comms: Comms, caller: ToolCaller) -> HostLogRead:
        if not 1 <= self.lines <= self.longest:
            raise ValueError(f"lines must be from 1 to {self.longest}, not {self.lines}")
        return HostLogRead(self.source.read(comms.root, self.lines))


@dataclass(frozen=True, kw_only=True)
class CommsWorkBranchesTool(ReadTool, NativeToolRequest):
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
    description = (
        "Rename your own persistent thread to a new display name; the thread name is that"
        " text's slug and old names remain routing aliases."
    )
    display_name: Annotated[str, *tool_field("Your new display name")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> RenameThreadResult:
        return comms.threads.rename_self(self.display_name, caller=caller)


@dataclass(frozen=True, kw_only=True)
class CommsSetOwnThreadTitleTool(ThreadToolRequest):
    """A thread corrects its own display title without touching its identity."""

    label = "Set Comms Thread Title"
    description = (
        "Correct your own thread's display title; its name and routing aliases stay unchanged."
    )
    title: Annotated[str, *tool_field("Your new display title")]

    def apply(self, comms: Comms, caller: ThreadCaller) -> ThreadTitleResult:
        return comms.threads.set_own_title(self.title, caller=caller)


@dataclass(frozen=True)
class SendRefusalsRead(ToolResult):
    refusals: tuple[SendRefusal, ...]


@dataclass(frozen=True, kw_only=True)
class CommsSendRefusalsTool(ReadTool, NativeToolRequest):
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
class SendTool(PublishingTool[ThreadCaller, MessagePublished]):
    label = "Comms Send"
    description = (
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text. "
        "External sends may name received originals in answers; "
        "source-turn identity is not a model argument."
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

    answers: Annotated[
        tuple[MessageReference, ...],
        *tool_field("Original received messages explicitly answered; external sends without a named source turn must name these to link answers"),
    ] = ()

    def publish(self, comms: Comms, caller: ThreadCaller) -> MessagePublished:
        sender = caller.own_thread()
        target = CommandTarget.named(self.target)
        try:
            caller.require_directive(comms, self.body)
            body = target.addressed(self.audience, self.body)
            result = comms.messaging.send(
                sender, target.name, body, self.type, report=self.report,
                task=self.attachment(comms, target), origin=caller.origin(),
                answers=self.answers)
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

    native_call = CommunicationCall


@dataclass(frozen=True, kw_only=True)
class CommsUserSendTool(PublishingToolRequest[ToolCaller, MessagePublished]):
    """Local and connector UI input use the existing durable human publication."""

    label = "Send human input"
    description = ("Publish input typed by the owner to a thread or channel under the owner's "
                   "durable user identity; requires a human UI caller.")
    target: Annotated[str, *tool_field("Target thread name or channel", wire_name="to")]
    body: Annotated[str, *tool_field("Human input text")]

    def publish(self, comms: Comms, caller: ToolCaller) -> MessagePublished:
        caller.require_human()
        message = comms.messaging.send_user_message(
            CommandTarget.named(self.target), self.body, worktree=Checkout(comms.root))
        return MessagePublished.from_send(SendResult(message, CorrectionComplete()))


@dataclass(frozen=True)
class ThreadViewMarked(ToolResult):
    """The thread whose displayed position the owner acknowledged."""

    thread: str


@dataclass(frozen=True, kw_only=True)
class CommsMarkThreadViewReadTool(HumanToolRequest[ThreadViewMarked]):
    """A UI acknowledges what it displayed of one thread, as the owner, locally or across
    the connector; requires a human UI caller."""

    label = "Mark thread view read"
    description = ("Acknowledge the transcript position a UI displayed for one thread, through "
                   "the thread's receiving contract, as the owner; requires a human UI caller.")
    thread: Annotated[str, *tool_field("Thread name")]
    through: Annotated[TranscriptCursor, *tool_field("Displayed transcript position")]

    def answered(self, comms: Comms) -> ThreadViewMarked:
        comms.views.mark_thread_view_read(self.thread, worktree=Checkout(comms.root), through=self.through)
        return ThreadViewMarked(self.thread)


@dataclass(frozen=True, kw_only=True)
class CommsAuthoredTaskTool(ThreadToolRequest, PublishingToolRequest[ThreadCaller, TaskPublished]):
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
class CommsHistoryTool(ReadTool, NativeToolRequest):
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
            me=caller.history_viewer(self.participant),
            limit=self.limit, before=self.before,
        ).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsInboxTool(NativeToolRequest[ToolCaller, InboxPageRead]):
    label = "Comms Inbox"
    description = (
        "Fetch one oldest-first page of messages no harness attempted and no native input took, "
        "and the unresolved input attempts, for a thread. Optional target limits the conversation. "
        "Reading records nothing: a message leaves the page when its harness takes it, never "
        "because a page showed it, and human read markers are never touched. remaining counts "
        "the untaken messages after this page. One oversized message is returned alone for "
        "progress. Unresolved inputs remain UNKNOWN and are not paged. For standby, supply standby "
        "(goal_id and wait_for) to get standby_review: exact eligible keys and messages, "
        "already reviewed entries, and excluded owner/other-dependency inputs."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    target: Annotated[str, *tool_field("Channel or peer conversation")] = AnyChannel.address()
    limit: Annotated[int, *tool_field("Maximum messages in this page; must be positive")] = (
        100
    )
    standby: Annotated[
        StandbyRequest | NoStandby, *tool_field("Goal and dependencies to review")
    ] = NoStandby()
    after: Annotated[
        TakenInputPosition, *tool_field("Continue after this taken message; default the oldest")
    ] = TakenInputStart()
    wait: Annotated[
        InboxWait, *tool_field("Seconds to wait for a first message; default answer now")
    ] = NoWait()

    def apply(self, comms: Comms, caller: ToolCaller) -> InboxPageRead:
        thread = self.thread
        review = self.standby.review(comms, thread)
        page = self.page(comms)
        return InboxPageRead(
            page.page.messages, page.remaining, tuple(comms.goals.unresolved_inputs(thread)),
            standby_review=review,
        )

    def page(self, comms: Comms) -> InboxPage:
        return self.waited(comms, comms.bus.inbox_at)

    def messages(self, comms: Comms) -> tuple[Message, ...]:
        """The messages alone: a reader that shows no unresolved inputs and no remainder need
        not project them."""
        return self.waited(comms, comms.bus.untaken_at).page.messages

    def waited[Page: DisplayedMessagePage](self, comms: Comms, read: Callable[..., Page]) -> Page:
        if not (target := self.target.strip()):
            raise ToolRefusedError("An inbox target cannot be empty.")
        return self.wait.read(comms.bus.bus, lambda: read(
            self.thread, comms.registry.snapshot(), CommandTarget.named(target),
            after=self.after, bounds=PageBounds(limit=self.limit),
        ))


@dataclass(frozen=True, kw_only=True)
class CommsRestartQueueTool(ReadTool, NativeToolRequest):
    label = "Restart Queue Status"
    description = "Inspect queued, restarted, stale, or uncertain attempts for a thread."
    name: Annotated[str, *tool_field("Agent thread")]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartAttempts:
        return RestartAttempts(tuple(RestartQueue(comms.root).history(
            comms.registry.require(self.name).incarnation.require_recorded())))


@dataclass(frozen=True, kw_only=True)
class CommsCancelRestartTool(NativeToolRequest):
    label = "Cancel Queued Restart"
    description = "Cancel pending restarts for a thread; cannot undo an attempted restart."
    name: Annotated[str, *tool_field("Agent thread")]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartsCancelled:
        return RestartsCancelled(tuple(RestartQueue(comms.root).cancel(
            comms.registry.require(self.name).incarnation.require_recorded())))


@dataclass(frozen=True, kw_only=True)
class CommsForkTool(LiveOwnerTool, NativeOwnerCommand, ThreadContextAction):
    label = "Comms Fork"
    description = "Fork a child agent thread from a registered parent's persistent session."
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


@dataclass(frozen=True)
class InstallLaunches(ToolResult):
    """One line per configured main the install covers: launched, already running, or why not."""

    launches: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CommsInstallTool(NativeToolRequest):
    label = "Install Latest Mains"
    description = (
        "Launch Core's installer for every configured main the installed build lacks: the same "
        "launch as `order install`. Returns once the install's unit is running; an install of "
        "the same target already running counts as this launch. The install stops and restarts "
        "teammates and the connector and posts its result to #comms."
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> InstallLaunches:
        return InstallLaunches(InstallDue.launch_due(comms))


@dataclass(frozen=True, kw_only=True)
class CommsStopTool(NativeOwnerCommand, OwnerLifecycleControl, ThreadContextAction):
    label = "Stop Comms Thread"
    description = "Stop a thread after verifying that its process owns the registered identity."
    action_label = "Stop process"
    name: Annotated[str, *tool_field("Thread name", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadStoppedResult:
        return StopCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsStartTool(LiveOwnerTool, NativeOwnerCommand, StartOwnerControl, ThreadContextAction):
    label = "Start Comms Thread"
    description = (
        "Start a stopped agent thread with its saved conversation and configuration. "
        "Reuses an already-running owner without interrupting it. The returned PID is "
        "reserved; startup may still be in progress. Does not send a new task."
    )
    action_label = "Start thread"
    name: Annotated[str, *tool_field("Thread to start", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> OwnerStartResult:
        return StartCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsQueueRestartTool(
    LiveOwnerTool, NativeOwnerCommand, OwnerLifecycleControl, ThreadContextAction,
):
    label = "Queue Idle Owner Restart"
    description = (
        "Queue an exact live owner incarnation for restart when idle. Never interrupt an active "
        "turn. A changed owner or uncertain attempt is never retried automatically. "
        "Restarts in the calling process's runtime; does not deploy another runtime."
    )
    action_label = "Queue idle restart"
    name: Annotated[str, *tool_field("Live agent thread", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartQueued:
        return QueueRestartCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(ArchiveOwnerControl, ThreadContextAction):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    action_label = "Archive stopped thread"
    name: Annotated[str, *tool_field("Stopped thread name", binding=SubjectBinding)]

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadArchivedResult:
        return ArchiveCliCommand(name=self.name).apply(comms)




@dataclass(frozen=True)
class ToolReplyResult(CliResult):
    """The tool protocol's own reply, the one every adapter receives, written unwrapped."""

    reply: dict[str, JsonValue]

    def to_wire(self) -> dict[str, JsonValue]:
        return self.reply




def invoke_tool(
    comms: Comms, name: str, arguments: Mapping[str, object], *, caller: ToolCaller
) -> dict[str, JsonValue]:
    return ToolRequest.member_named(name).invoke(comms, arguments, caller=caller)
