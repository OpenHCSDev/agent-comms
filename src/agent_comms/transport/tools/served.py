from __future__ import annotations

import asyncio
import dataclasses
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import JsonValue

from agent_comms.transport.sync_answer import AwaitsWork
from agent_comms.acp_extension import CoordinationChangedUpdate
from agent_comms.cli_bridges.history import OfflineHistory
from agent_comms.errors import SourceOutsideImport
from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_verbs.cli_command import PreparedTargetAction, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import (
    ThreadArchivedResult,
    ThreadCreatedResult,
    ThreadStoppedResult,
)
from agent_comms.cli_verbs.cli_result import CliResult, TargetEditResult
from agent_comms.cli_verbs.delete_view import DeleteViewCliCommand
from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.cli_verbs.history import HistoryCliCommand
from agent_comms.cli_verbs.pin_channel import PinChannelCliCommand
from agent_comms.cli_verbs.pin_thread import PinThreadCliCommand
from agent_comms.cli_verbs.queue_restart import QueueRestartCliCommand
from agent_comms.cli_verbs.start import StartCliCommand
from agent_comms.cli_verbs.stop import StopCliCommand
from agent_comms.cli_verbs.threads import ThreadsCliCommand
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions
from agent_comms.coordination.tables.refusals import SendRefusal
from agent_comms.coordination.working_memory_annotations import WorkingMemoryAnnotations
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.imported_session_metadata import ImportedSessionMetadata
from agent_comms.execution.input_origin import HumanInputOrigin
from agent_comms.execution.owner_lifecycle import OwnerStartResult
from agent_comms.execution.pi_vocabulary import ServiceTier, ThinkingLevelField
from agent_comms.execution.restart_queue import QueuedRestart, RestartQueue, RestartQueued
from agent_comms.execution.turn_context import ContextManifest, ContextSourceText, SegmentManifest
from agent_comms.history_views import RetiredViews
from agent_comms.importing import ThreadImported
from agent_comms.messaging.bus_identity import BusIdentityStore
from agent_comms.messaging.channel_management import ListTagEdit, TagEdit
from agent_comms.messaging.channel_targets import ChannelTarget, CommandTarget
from agent_comms.messaging.channels import Channel, SavedView, ViewMatch, ViewPredicate
from agent_comms.messaging.display_references import ReferenceNames
from agent_comms.messaging.exporting import WireExportProvenance
from agent_comms.messaging.historical_views import ConversationRevision
from agent_comms.messaging.history_result import HistoryPageResult
from agent_comms.messaging.message_notification import MessageNotification
from agent_comms.messaging.message_page import MessagePage
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message, NoticeRole
from agent_comms.messaging.notification_evidence import (
    AfterRecipient,
    FirstRecipient,
    RecipientCursor,
    RecipientLimit,
)
from agent_comms.messaging.notification_reads import LiveNotificationRead
from agent_comms.messaging.page_traversal import (
    LatestSequence,
    LiveCursor,
    NewestCursor,
    PageCursor,
)
from agent_comms.messaging.send_result import (
    CorrectionComplete,
    CorrectionResult,
    MessagePublished,
    SendResult,
)
from agent_comms.messaging.task_sources import (
    Constraint,
    Decision,
    OriginalTaskChange,
    Subtask,
    TaskAttachment,
    TaskChange,
)
from agent_comms.nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    ContextSource,
    FieldCodec,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    WireForm,
    WireName,
    WireOmitDefault,
)
from agent_comms.planning.task_scope import CurrentTaskScopeSelection, TaskScopeSelection
from agent_comms.presentation import ChannelView, WireRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.work_branches import WorkBranch, WorkBranches
from agent_comms.registry.registry_document import (
    ProjectThreadSelection,
    RegistrySlice,
    RegistrySnapshot,
)
from agent_comms.registry.relationships import (
    Collaboration,
    GoalDerivedContact,
    GoalMentionDiagnostic,
    RelationshipEdit,
    ThreadCommsSnapshot,
)
from agent_comms.registry.thread_identity import ThreadIncarnation, ToolName
from agent_comms.registry.thread_management import (
    InheritedTags,
    ProjectChangeResult,
    RenameThreadResult,
    TagChoice,
    ThreadTitleResult,
)
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.transcript.transcripts import NotifiedThreadPresentation
from agent_comms.registry.threads import (
    ArchiveOwnerControl,
    NativeOwnerCommand,
    NextTurnConfiguration,
    OwnerLifecycleControl,
    StartOwnerControl,
)
from agent_comms.team.standing import InstallDue
from agent_comms.transport.host_logs import HostLog, HostLogRead
from agent_comms.transport.native_tools import CommunicationCall
from agent_comms.transport.owner_connection import OwnerConnection
from agent_comms.transport.request import SubjectBinding, tool_field
from agent_comms.transport.runtime_requests import QueueActionRuntimeRequest
from agent_comms.transport.serving.owner_launch import HostResources
from agent_comms.transport.tool_caller import ThreadCaller, ThreadListing, ToolCaller
from agent_comms.transport.tool_declaration import NativeRequestTools
from agent_comms.transport.tool_request import (
    CoreRead,
    HumanToolRequest,
    LiveOwnerTool,
    NativeToolRequest,
    ObservedToolRequest,
    PublishingToolRequest,
    ReadTool,
    ServedRequest,
    ThreadContextAction,
    ThreadReadTool,
    ThreadToolRequest,
)
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools import channels
from agent_comms.transport.tools.channels import ChannelListing
from agent_comms.transport.tools.sidebar import (
    ChannelOrderSet,
    CommsCaptureHumanInputTool,
    CommsChannelTool,
    CommsContextInspectionTool,
    CommsConversationPageTool,
    CommsConversationRevisionTool,
    CommsExportWireTool,
    CommsHistoricalThreadsTool,
    CommsImportedSourceTool,
    CommsImportThreadTool,
    CommsLatestSequenceTool,
    CommsMarkPageReadTool,
    CommsMarkThreadViewReadTool,
    CommsMoveThreadProjectTool,
    CommsOwnerRequestTool,
    CommsPrepareTargetActionTool,
    CommsQueueActionTool,
    CommsReferenceNamesTool,
    CommsRegistrySliceTool,
    CommsRegistryTool,
    CommsRenameManagedThreadTool,
    CommsRetiredViewsTool,
    CommsSavedSegmentTool,
    CommsSavedSourceTool,
    CommsServedRootTool,
    CommsSessionThreadTool,
    CommsSetRelationshipOrderTool,
    CommsSortChannelsTool,
    CommsSortChannelTool,
    CommsTargetActionsTool,
    CommsTargetEditTool,
    CommsThreadConfigurationTool,
    CommsThreadPresentationTool,
    CommsThreadRelationshipsTool,
    CommsUserSendTool,
    CommsWireRevisionTool,
    ContextInspectionRead,
    HistoricalThreadsRead,
    OwnerAnswered,
    PageMarked,
    QueueActionApplied,
    RelationshipOrdered,
    SavedManifestRequest,
    ServedRoot,
    TargetActionsRead,
    ThreadViewMarked,
)
from agent_comms.working_memory.working_memory_labels import JevClassifier
from agent_comms_hooks import ToolRefusedError


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
class CommsDiscoverTool(ReadTool, NativeToolRequest):
    """The native adapter activates these declarations through Pi's existing tool loader."""

    label = "Find and load Comms tools"
    description = (
        "Find and load any Comms operation before calling it. Search by words in its name "
        "or description, or select exact names. An explicitly empty search loads every tool. "
        "Tools stay active for this turn, not for the session's lifetime. "
        "Use this for messages, inbox, goals, orders, reviews, threads, channels and configuration."
    )
    @classmethod
    def loads_tools(cls) -> bool:
        return True
    native_call = CommunicationCall
    selection: Annotated[ToolLookup, *tool_field("Search words or select exact tool names")]

    def apply(self, comms: Comms, caller: ToolCaller) -> ToolsSelected:
        return ToolsSelected(tuple(AvailableTool(ToolName(tool.declared_name), tool.label, tool.description)
                                   for tool in self.selection.selected(NativeRequestTools.members_with(NativeRequestTools))))


class ServedCommsChannelTool(CoreRead[Channel], CommsChannelTool):
    """Serving CommsChannelTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return comms.bus.require_channel(self.name)


class ServedCommsHistoricalThreadsTool(CoreRead[HistoricalThreadsRead], CommsHistoricalThreadsTool):
    """Serving CommsHistoricalThreadsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> HistoricalThreadsRead:
        return HistoricalThreadsRead(comms.bus.history.threads(self.senders))


@dataclass(frozen=True, kw_only=True)
class ServedCommsExportWireTool(
    ObservedToolRequest[ToolCaller, WireExportProvenance],
    HumanToolRequest[WireExportProvenance],
    CommsExportWireTool,
):
    """Serving CommsExportWireTool. ObservedToolRequest is itself a dataclass, so this member
    is one too: otherwise its fields would hide the declaration's."""

    def answered(self, comms: Comms) -> WireExportProvenance:
        return comms.views.export_wire(self.destination, format=self.format, scope=self.scope,
                                       limit=self.limit, observer=self.observer)


class ServedCommsImportThreadTool(HumanToolRequest[ThreadImported], CommsImportThreadTool):
    """Serving CommsImportThreadTool."""

    def answered(self, comms: Comms) -> ThreadImported:
        return comms.threads.import_thread(OfflineHistory.stored(self.harness, self.source, self.session),
                                          name=self.name, worktree=self.worktree)


class ServedCommsContextInspectionTool(CoreRead[ContextInspectionRead], CommsContextInspectionTool):
    """Serving CommsContextInspectionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextInspectionRead:
        entry = comms.registry.entry(self.owner).entry
        thread = entry.thread
        page = comms.contexts.page(self.owner, comms.registry, cursor=self.cursor, previous=self.previous)
        annotations = WorkingMemoryAnnotations.for_context(
            comms.registry.store.coordinator_reader,
            tuple(resource.value for resource in page.resources), JevClassifier.version())
        return ContextInspectionRead(
            comms.root, entry, page, thread.runtime.session_observation(comms.root, thread), annotations,
            ImportedSessionMetadata.sources_for_owner(comms.registry, thread))


class ServedCommsImportedSourceTool(CoreRead[ContextSourceText], CommsImportedSourceTool):
    """Serving CommsImportedSourceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextSourceText:
        if self.source not in ImportedSessionMetadata.sources_for_owner(comms.registry, self.owner):
            raise SourceOutsideImport("Source is outside the original imported snapshot")
        return ContextSourceText(self.source.public_description(), self.source.public_text(comms))


class SavedManifestRead[R: ToolResult = ToolResult](
    CoreRead[R], SavedManifestRequest[R], declared_name=None,
):
    """Serving a read of one retained context manifest of a stopped thread."""

    def manifest(self, comms: Comms) -> ContextManifest:
        return self.reference.read(comms.contexts, self.owner, comms.registry)


class ServedCommsSavedSourceTool(SavedManifestRead[ContextSourceText], CommsSavedSourceTool):
    """Serving CommsSavedSourceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextSourceText:
        return self.manifest(comms).public_source_text(comms, self.position, self.source)


class ServedCommsSavedSegmentTool(SavedManifestRead[SegmentManifest], CommsSavedSegmentTool):
    """Serving CommsSavedSegmentTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> SegmentManifest:
        return self.manifest(comms).selected_segment(self.position, self.contributors)


class ServedCommsOwnerRequestTool(HumanToolRequest[OwnerAnswered], CommsOwnerRequestTool):
    """Serving CommsOwnerRequestTool."""

    def answered(self, comms: Comms) -> OwnerAnswered:
        thread = comms.registry.require(self.owner.name)
        if thread.incarnation != self.owner:
            raise ValueError("Selected thread incarnation changed")
        return OwnerAnswered(FieldCodec.decode(JsonValue, OwnerConnection.exchange_at(
            comms.root, thread, self.request)))


class ServedCommsMoveThreadProjectTool(HumanToolRequest[ProjectChangeResult], CommsMoveThreadProjectTool):
    """Serving CommsMoveThreadProjectTool."""

    def answered(self, comms: Comms) -> ProjectChangeResult:
        return comms.threads.set_project(self.owner, self.path)


class ServedCommsRenameManagedThreadTool(HumanToolRequest[RenameThreadResult], CommsRenameManagedThreadTool):
    """Serving CommsRenameManagedThreadTool."""

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


class ServedCommsCaptureHumanInputTool(HumanToolRequest[HumanInputOrigin], CommsCaptureHumanInputTool):
    """Serving CommsCaptureHumanInputTool."""

    def answered(self, comms: Comms) -> HumanInputOrigin:
        return HumanInputOrigin.capture(comms, self.claimed_identity)


class ServedCommsThreadPresentationTool(CoreRead[NotifiedThreadPresentation], CommsThreadPresentationTool):
    """Serving CommsThreadPresentationTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> NotifiedThreadPresentation:
        return comms.views.thread_presentations((self.thread,)).read(self.thread).published()


class ServedCommsQueueActionTool(HumanToolRequest[QueueActionApplied], CommsQueueActionTool):
    """Serving CommsQueueActionTool."""

    def answered(self, comms: Comms) -> QueueActionApplied:
        original = comms.registry.snapshot().born(self.lookup)
        QueueActionRuntimeRequest.serving(self.action).at_owner(comms.root, original)
        return QueueActionApplied(original.name)


class ServedCommsWireRevisionTool(CoreRead[WireRevision], CommsWireRevisionTool):
    """Serving CommsWireRevisionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> WireRevision:
        return comms.views.revision()


class ServedCommsMarkPageReadTool(HumanToolRequest[PageMarked], CommsMarkPageReadTool):
    """Serving CommsMarkPageReadTool."""

    def answered(self, comms: Comms) -> PageMarked:
        comms.views.mark_page_read(self.page, worktree=self.worktree)
        return PageMarked(len(self.page.messages))


class ServedCommsThreadRelationshipsTool(CoreRead[ThreadCommsSnapshot], CommsThreadRelationshipsTool):
    """Serving CommsThreadRelationshipsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadCommsSnapshot:
        return comms.relationships.snapshot(self.owner)


class ServedCommsSetRelationshipOrderTool(HumanToolRequest[RelationshipOrdered], CommsSetRelationshipOrderTool):
    """Serving CommsSetRelationshipOrderTool."""

    def answered(self, comms: Comms) -> RelationshipOrdered:
        return RelationshipOrdered(comms.relationships.set_order(self.owner, self.group, self.order))


class ServedCommsTargetActionsTool(CoreRead[TargetActionsRead], CommsTargetActionsTool):
    """Serving CommsTargetActionsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> TargetActionsRead:
        return TargetActionsRead(TargetActionCommand.target_catalog(
            comms, TargetActionCommand.selected_targets(self.targets), self.channel, project=self.project))


class ServedCommsPrepareTargetActionTool(
    CoreRead[PreparedTargetAction], CommsPrepareTargetActionTool, AwaitsWork
):
    """Serving CommsPrepareTargetActionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> PreparedTargetAction:
        return self.run_awaited(self.action.prepared(ConfigOptions.for_route(comms.route)))


class ServedCommsTargetEditTool(HumanToolRequest[TargetEditResult | Channel], CommsTargetEditTool):
    """Serving CommsTargetEditTool."""

    def answered(self, comms: Comms) -> TargetEditResult | Channel:
        return self.edit.apply(comms)


class ServedCommsRetiredViewsTool(CoreRead[RetiredViews], CommsRetiredViewsTool):
    """Serving CommsRetiredViewsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RetiredViews:
        return comms.views.retired_views(self.threads, self.channels)


class ServedCommsThreadConfigurationTool(CoreRead[CoordinationChangedUpdate], CommsThreadConfigurationTool):
    """Serving CommsThreadConfigurationTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> CoordinationChangedUpdate:
        return comms.configuration.answer(self.thread)


class ServedCommsSessionThreadTool(CoreRead[ProjectThreadSelection], CommsSessionThreadTool):
    """Serving CommsSessionThreadTool."""

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
        *tool_field("Thread to change (defaults to the executing thread)", ContextSource(SubjectBinding)),
    ] = CallerSubject()

    def apply(self, comms: Comms, caller: ToolCaller) -> ModelChanged:
        """Change this thread's model or another thread's model for future turns."""
        thread_name = self.thread.name(caller)
        original = comms.registry.require(thread_name)
        thread = comms.threads.set_thread_configuration(original, self)
        return ModelChanged(thread=thread.name, model=thread.model, thinking_level=thread.thinking_level, service_tier=thread.service_tier)


class ServedCommsSortChannelsTool(NativeToolRequest, CommsSortChannelsTool):
    """Serving CommsSortChannelsTool."""

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


class ServedCommsConversationPageTool(CoreRead[MessagePage], CommsConversationPageTool):
    """Serving CommsConversationPageTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> MessagePage:
        history = self.conversation.display_history(comms.views, self.project)
        return history.page(comms.views.presentation, self.bounds)


class ServedCommsConversationRevisionTool(CoreRead[ConversationRevision], CommsConversationRevisionTool):
    """Serving CommsConversationRevisionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ConversationRevision:
        history = self.conversation.display_history(comms.views, self.project)
        return history.revision(comms.views, self.conversation, self.seen, worktree=self.project)


class ServedCommsLatestSequenceTool(CoreRead[LatestSequence], CommsLatestSequenceTool):
    """Serving CommsLatestSequenceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> LatestSequence:
        return LatestSequence(LiveCursor(comms.views.latest_sequence()))


class ServedCommsServedRootTool(CoreRead[ServedRoot], CommsServedRootTool):
    """Serving CommsServedRootTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ServedRoot:
        root = comms.root.resolve()
        return ServedRoot(str(root), BusIdentityStore.serving(root))


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
        return channels.CommsChannelsTool().apply(comms, caller)


class ServedCommsSortChannelTool(NativeToolRequest, CommsSortChannelTool):
    """Serving CommsSortChannelTool."""

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
            previous=result.previous, current=result.current, changed=result.changed,
            thread=result.thread,
            instruction=(
                (
                    "Project saved. End this turn now; the runtime automatically resumes this same "
                    "conversation in the new project with refreshed tools and context."
                )
                if result.changed
                else "This is already your project directory."
            ),
        )


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


class ServedCommsRegistryTool(CoreRead[RegistrySnapshot], CommsRegistryTool):
    """Serving CommsRegistryTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RegistrySnapshot:
        return comms.registry.snapshot()


class ServedCommsRegistrySliceTool(CoreRead[RegistrySlice], CommsRegistrySliceTool):
    """Serving CommsRegistrySliceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RegistrySlice:
        return comms.registry.snapshot().only(self.threads)


class ServedCommsReferenceNamesTool(CoreRead[ReferenceNames], CommsReferenceNamesTool):
    """Serving CommsReferenceNamesTool."""

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


class ServedCommsUserSendTool(PublishingToolRequest[ToolCaller, MessagePublished], CommsUserSendTool):
    """Serving CommsUserSendTool."""

    def publish(self, comms: Comms, caller: ToolCaller) -> MessagePublished:
        caller.require_human()
        message = comms.messaging.send_user_message(
            CommandTarget.named(self.target), self.body, worktree=Checkout(comms.root))
        return MessagePublished.from_send(SendResult(message, CorrectionComplete()))


class ServedCommsMarkThreadViewReadTool(HumanToolRequest[ThreadViewMarked], CommsMarkThreadViewReadTool):
    """Serving CommsMarkThreadViewReadTool."""

    def answered(self, comms: Comms) -> ThreadViewMarked:
        comms.views.mark_thread_view_read(self.thread, worktree=Checkout(comms.root), through=self.through)
        return ThreadViewMarked(self.thread)


@dataclass(frozen=True, kw_only=True)
class CommsAuthoredTaskTool(ThreadToolRequest, PublishingToolRequest[ThreadCaller, TaskPublished]):
    """One original authorized publication path for authored task declarations."""

    target: Annotated[
        str, *tool_field("Thread or channel receiving the original declaration", WireName("to"))
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
    parent: Annotated[str, *tool_field("Parent thread name", ContextSource(SubjectBinding))]
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
    name: Annotated[str, *tool_field("Thread name", ContextSource(SubjectBinding))]

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
    name: Annotated[str, *tool_field("Thread to start", ContextSource(SubjectBinding))]

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
    name: Annotated[str, *tool_field("Live agent thread", ContextSource(SubjectBinding))]

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartQueued:
        return QueueRestartCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(ArchiveOwnerControl, ThreadContextAction):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    action_label = "Archive stopped thread"
    name: Annotated[str, *tool_field("Stopped thread name", ContextSource(SubjectBinding))]

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
    return ServedRequest.member_named(name).invoke(comms, arguments, caller=caller)
