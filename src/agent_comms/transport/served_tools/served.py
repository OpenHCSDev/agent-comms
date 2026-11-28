"""Server-only implementations of sidebar and command tools."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from pydantic import JsonValue

from agent_comms.acp_extension import CoordinationChangedUpdate
from agent_comms.cli_bridges.history import OfflineHistory
from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_verbs.cli_command import PreparedTargetAction, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import (
    ThreadArchivedResult,
    ThreadCreatedResult,
    ThreadStoppedResult,
)
from agent_comms.cli_verbs.cli_inbox import (
    CliInboxCliCommand,
    ThreadExecutionResult,
)
from agent_comms.cli_verbs.cli_result import CliResult, TargetEditResult
from agent_comms.cli_verbs.delete_view import DeleteViewCliCommand
from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.cli_verbs.history import HistoryCliCommand
from agent_comms.cli_verbs.native_thread import NativeThreadCliCommand
from agent_comms.cli_verbs.pin_channel import PinChannelCliCommand
from agent_comms.cli_verbs.pin_thread import PinThreadCliCommand
from agent_comms.cli_verbs.queue_restart import QueueRestartCliCommand
from agent_comms.cli_verbs.start import StartCliCommand
from agent_comms.cli_verbs.stop import StopCliCommand
from agent_comms.cli_verbs.threads import ThreadsCliCommand
from agent_comms.comms import Comms
from agent_comms.coordination.tables.refusals import SendRefusal
from agent_comms.coordination.working_memory_annotations import WorkingMemoryAnnotations
from agent_comms.errors import SourceOutsideImport
from agent_comms.execution.imported_session_metadata import ImportedSessionMetadata
from agent_comms.execution.input_origin import HumanInputOrigin
from agent_comms.execution.owner_lifecycle import OwnerStartResult
from agent_comms.execution.restart_queue import QueuedRestart, RestartQueue, RestartQueued
from agent_comms.execution.turn_context import ContextManifest, ContextSourceText, SegmentManifest
from agent_comms.messaging.bus_identity import BusIdentityStore
from agent_comms.messaging.channel_targets import ChannelTarget, CommandTarget
from agent_comms.messaging.channels import Channel, SavedView, ViewPredicate
from agent_comms.messaging.display_references import ReferenceNames
from agent_comms.messaging.exporting import WireExportProvenance
from agent_comms.messaging.historical_views import ChannelHistory, ConversationRevision, DMHistory
from agent_comms.messaging.history_result import HistoryPageResult
from agent_comms.messaging.message_files import MessageFileContent, MessageFilesRead
from agent_comms.messaging.message_page import MessagePage
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import NoticeRole
from agent_comms.messaging.notification_evidence import AfterRecipient
from agent_comms.messaging.notification_reads import LiveNotificationRead
from agent_comms.messaging.page_traversal import LatestSequence, LiveCursor
from agent_comms.messaging.published_message import MessagePublished
from agent_comms.messaging.send_result import (
    CorrectionComplete,
    SendResult,
)
from agent_comms.messaging.task_sources import Constraint, Decision, Subtask
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.presentation import ChannelView, WireRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.registry.registry_document import (
    ProjectThreadSelection,
    RegistrySlice,
    RegistrySnapshot,
)
from agent_comms.registry.relationships import ThreadCommsSnapshot
from agent_comms.registry.thread_identity import ToolName
from agent_comms.registry.thread_model import UndeclaredModel
from agent_comms.registry.thread_results import (
    ProjectChangeResult,
    RenameThreadResult,
    ThreadImported,
    ThreadTitleResult,
)
from agent_comms.registry.threads import (
    ArchiveOwnerControl,
    ConfigurableThreadCommand,
    ExternalOwnerCommand,
    ForkableThreadCommand,
    NativeOwnerCommand,
    OwnerLifecycleControl,
    StartOwnerControl,
)
from agent_comms.team.standing import InstallDue
from agent_comms.transcript.transcripts import NotifiedThreadPresentation
from agent_comms.transport.host_logs import HostLogRead
from agent_comms.transport.native_tools import CommunicationCall
from agent_comms.transport.owner_connection import OwnerConnection
from agent_comms.transport.runtime_requests import QueueActionRuntimeRequest
from agent_comms.transport.runtime_server import ServedRuntimeRequest
from agent_comms.transport.serving.owner_launch import HostResources
from agent_comms.transport.sync_answer import AwaitsWork
from agent_comms.transport.tool_caller import ThreadCaller, ThreadListing, ToolCaller
from agent_comms.transport.tool_declaration import NativeRequestTools
from agent_comms.transport.tool_request import (
    ChannelsArea,
    CoreRead,
    GoalsArea,
    HostArea,
    HumanToolRequest,
    LiveOwnerTool,
    MessagesArea,
    NativeToolRequest,
    ObservedToolRequest,
    PublishingToolRequest,
    ReadTool,
    ScreensArea,
    ServedRequest,
    ShortListed,
    ThreadContextAction,
    ThreadReadTool,
    ThreadsArea,
    ThreadToolRequest,
    WorkArea,
)
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools import background  # registers the background run family
from agent_comms.transport.tools import channels
from agent_comms.transport.tools import inbox  # registers comms_inbox and comms_ack
from agent_comms.transport.tools.channels import ChannelListing
from agent_comms.transport.tools.commands import (
    AuthoredTaskRequest,
    AvailableTool,
    CollaborationEdited,
    CollaborationList,
    CommsArchiveTool,
    CommsCancelRestartTool,
    CommsCliInboxTool,
    CommsCollaborationTool,
    CommsCollaborationsTool,
    CommsConstraintTool,
    CommsDecisionTool,
    CommsDeleteViewTool,
    CommsDiscoverTool,
    CommsForkTool,
    CommsHistoryTool,
    CommsHostLogTool,
    CommsInstallTool,
    CommsMessageOutcomesTool,
    CommsModelTool,
    CommsNativeThreadTool,
    CommsPinChannelTool,
    CommsPinThreadTool,
    CommsQueueRestartTool,
    CommsRenameSelfTool,
    CommsResourcesTool,
    CommsRestartQueueTool,
    CommsSendRefusalsTool,
    CommsSetChannelMetadataTool,
    CommsSetOwnThreadTitleTool,
    CommsSetProjectTool,
    CommsSetThreadTitleTool,
    CommsSetViewTool,
    CommsStartTool,
    CommsStopTool,
    CommsSubtaskTool,
    CommsTagsTool,
    CommsThreadTagsTool,
    CommsThreadsTool,
    CommsWorkBranchesTool,
    InstallLaunches,
    MessageOutcomes,
    ModelChanged,
    ProjectSet,
    SendRefusalsRead,
    TagList,
    TaskPublished,
    ThreadTags,
    ToolsSelected,
    VisibleCollaborator,
    WorkBranchList,
)
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
    CommsAgentSettingsTool,
    CommsMarkThreadViewReadTool,
    CommsMessageFilesTool,
    CommsMessageFileTool,
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
    CommsSetAgentSettingsTool,
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
    MessageFileRead,
    OwnerAnswered,
    PageMarked,
    QueueActionApplied,
    RelationshipOrdered,
    SavedManifestRequest,
    ServedRoot,
    TargetActionsRead,
    ThreadViewMarked,
)
from agent_comms.registry.agent_settings import AgentSettings
from agent_comms.registry.agent_settings_wire import AgentSettingsScope, ScopedSettingsProjection
from agent_comms.ui_model.retired_views import RetiredViews
from agent_comms.working_memory.working_memory_labels import JevClassifier
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True)
class RestartAttempts(ToolResult):
    restarts: tuple[QueuedRestart, ...]


@dataclass(frozen=True)
class RestartsCancelled(ToolResult):
    cancelled: tuple[QueuedRestart, ...]


def _tag_set(value: str) -> frozenset[str]:
    return frozenset(tag.strip() for tag in value.split(",") if tag.strip())


class ServedCommsDiscoverTool(HostArea, ReadTool, NativeToolRequest, CommsDiscoverTool):
    """Serving CommsDiscoverTool."""

    @classmethod
    def loads_tools(cls) -> bool:
        return True

    @classmethod
    def offered_to(cls, grant: ToolCaller) -> bool:
        """It loads tools into a native harness for one turn; a connector's client has its
        own list and nothing for this to load into."""
        return False

    native_call = CommunicationCall

    def apply(self, comms: Comms, caller: ToolCaller) -> ToolsSelected:
        return ToolsSelected(
            tuple(
                AvailableTool(ToolName(tool.declared_name), tool.label, tool.description)
                for tool in self.selection.selected(
                    NativeRequestTools.members_with(NativeRequestTools)
                )
            )
        )


class ServedCommsChannelTool(ShortListed, ChannelsArea, CoreRead[Channel], CommsChannelTool):
    """Serving CommsChannelTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return comms.bus.require_channel(self.name)


class ServedCommsHistoricalThreadsTool(
    ThreadsArea, CoreRead[HistoricalThreadsRead], CommsHistoricalThreadsTool
):
    """Serving CommsHistoricalThreadsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> HistoricalThreadsRead:
        return HistoricalThreadsRead(comms.bus.history.threads(self.senders))


@dataclass(frozen=True, kw_only=True)
class ServedCommsExportWireTool(
    ScreensArea,
    ObservedToolRequest[ToolCaller, WireExportProvenance],
    HumanToolRequest[WireExportProvenance],
    CommsExportWireTool,
):
    """Serving CommsExportWireTool. ObservedToolRequest is itself a dataclass, so this member
    is one too: otherwise its fields would hide the declaration's."""

    def answered(self, comms: Comms) -> WireExportProvenance:
        return comms.views.export_wire(
            self.destination,
            format=self.format,
            scope=self.scope,
            limit=self.limit,
            observer=self.observer,
        )


class ServedCommsImportThreadTool(
    ScreensArea, HumanToolRequest[ThreadImported], CommsImportThreadTool
):
    """Serving CommsImportThreadTool."""

    def answered(self, comms: Comms) -> ThreadImported:
        return comms.threads.import_thread(
            OfflineHistory.stored(self.harness, self.source, self.session),
            name=self.name,
            worktree=self.worktree,
        )


class ServedCommsContextInspectionTool(
    ScreensArea, CoreRead[ContextInspectionRead], CommsContextInspectionTool
):
    """Serving CommsContextInspectionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextInspectionRead:
        entry = comms.registry.entry(self.owner).entry
        thread = entry.thread
        page = comms.contexts.page(
            self.owner, comms.registry, cursor=self.cursor, previous=self.previous
        )
        annotations = WorkingMemoryAnnotations.for_context(
            comms.registry.store.coordinator_reader,
            tuple(resource.value for resource in page.resources),
            JevClassifier.version(),
        )
        return ContextInspectionRead(
            comms.root,
            entry,
            page,
            thread.runtime.session_observation(comms.root, thread),
            annotations,
            ImportedSessionMetadata.sources_for_owner(comms.registry, thread),
        )


class ServedCommsImportedSourceTool(
    ScreensArea, CoreRead[ContextSourceText], CommsImportedSourceTool
):
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


class ServedCommsSavedSourceTool(
    ScreensArea, SavedManifestRead[ContextSourceText], CommsSavedSourceTool
):
    """Serving CommsSavedSourceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ContextSourceText:
        return self.manifest(comms).public_source_text(comms, self.position, self.source)


class ServedCommsSavedSegmentTool(
    ScreensArea, SavedManifestRead[SegmentManifest], CommsSavedSegmentTool
):
    """Serving CommsSavedSegmentTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> SegmentManifest:
        return self.manifest(comms).selected_segment(self.position, self.contributors)


class ServedCommsOwnerRequestTool(
    ScreensArea, HumanToolRequest[OwnerAnswered], CommsOwnerRequestTool
):
    """Serving CommsOwnerRequestTool."""

    def answered(self, comms: Comms) -> OwnerAnswered:
        snapshot = comms.registry.snapshot()
        thread = snapshot.require(self.owner.name)
        if thread.incarnation != self.owner:
            raise ValueError("Selected thread incarnation changed")
        exchange = partial(OwnerConnection.exchange_at, comms.root, thread, self.request)
        answer = thread.answering_binding(thread.owner_binding(snapshot)).select_answer(
            unowned=exchange,
            native=exchange,
            held=partial(ServedRuntimeRequest.serving(self.request).held_by_bus, comms, thread),
        )
        return OwnerAnswered(FieldCodec.decode(JsonValue, answer()))


class ServedCommsMoveThreadProjectTool(
    ScreensArea, HumanToolRequest[ProjectChangeResult], CommsMoveThreadProjectTool
):
    """Serving CommsMoveThreadProjectTool."""

    def answered(self, comms: Comms) -> ProjectChangeResult:
        return comms.threads.set_project(self.owner, self.path)


class ServedCommsRenameManagedThreadTool(
    ScreensArea, HumanToolRequest[RenameThreadResult], CommsRenameManagedThreadTool
):
    """Serving CommsRenameManagedThreadTool."""

    def answered(self, comms: Comms) -> RenameThreadResult:
        return comms.threads.rename_managed_thread(self.thread, self.display_name, owner=self.owner)


class ServedCommsSetThreadTitleTool(
    ScreensArea, HumanToolRequest[ThreadTitleResult], CommsSetThreadTitleTool
):
    """Serving CommsSetThreadTitleTool."""

    def answered(self, comms: Comms) -> ThreadTitleResult:
        return comms.threads.set_managed_title(self.thread, self.title, owner=self.owner)


class ServedCommsCaptureHumanInputTool(
    ScreensArea, HumanToolRequest[HumanInputOrigin], CommsCaptureHumanInputTool
):
    """Serving CommsCaptureHumanInputTool."""

    def answered(self, comms: Comms) -> HumanInputOrigin:
        return HumanInputOrigin.capture(comms, self.claimed_identity)


class ServedCommsThreadPresentationTool(
    ScreensArea, CoreRead[NotifiedThreadPresentation], CommsThreadPresentationTool
):
    """Serving CommsThreadPresentationTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> NotifiedThreadPresentation:
        return comms.views.thread_presentations((self.thread,)).read(self.thread).published()


class ServedCommsQueueActionTool(
    ScreensArea, HumanToolRequest[QueueActionApplied], CommsQueueActionTool
):
    """Serving CommsQueueActionTool."""

    def answered(self, comms: Comms) -> QueueActionApplied:
        original = comms.registry.snapshot().born(self.lookup)
        QueueActionRuntimeRequest.serving(self.action).at_owner(comms.root, original)
        return QueueActionApplied(original.name)


class ServedCommsWireRevisionTool(ScreensArea, CoreRead[WireRevision], CommsWireRevisionTool):
    """Serving CommsWireRevisionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> WireRevision:
        return comms.views.revision()


class ServedCommsMarkPageReadTool(ScreensArea, HumanToolRequest[PageMarked], CommsMarkPageReadTool):
    """Serving CommsMarkPageReadTool."""

    def answered(self, comms: Comms) -> PageMarked:
        comms.views.mark_page_read(self.page, worktree=self.worktree)
        return PageMarked(len(self.page.messages))


class ServedCommsThreadRelationshipsTool(
    ScreensArea, CoreRead[ThreadCommsSnapshot], CommsThreadRelationshipsTool
):
    """Serving CommsThreadRelationshipsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadCommsSnapshot:
        return comms.relationships.snapshot(self.owner)


class ServedCommsSetRelationshipOrderTool(
    ScreensArea, HumanToolRequest[RelationshipOrdered], CommsSetRelationshipOrderTool
):
    """Serving CommsSetRelationshipOrderTool."""

    def answered(self, comms: Comms) -> RelationshipOrdered:
        return RelationshipOrdered(comms.relationships.set_order(self.owner, self.group, self.order))


class ServedCommsTargetActionsTool(
    ScreensArea, CoreRead[TargetActionsRead], CommsTargetActionsTool
):
    """Serving CommsTargetActionsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> TargetActionsRead:
        return TargetActionsRead(
            TargetActionCommand.target_catalog(
                comms,
                TargetActionCommand.selected_targets(self.targets),
                self.channel,
                project=self.project,
            )
        )


class ServedCommsPrepareTargetActionTool(
    ScreensArea,
    CoreRead[PreparedTargetAction],
    CommsPrepareTargetActionTool,
    AwaitsWork,
):
    """Serving CommsPrepareTargetActionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> PreparedTargetAction:
        return self.run_awaited(self.action.prepared(comms.configuration_catalogs))


class ServedCommsTargetEditTool(
    ScreensArea, HumanToolRequest[TargetEditResult | Channel], CommsTargetEditTool
):
    """Serving CommsTargetEditTool."""

    def answered(self, comms: Comms) -> TargetEditResult | Channel:
        return self.edit.apply(comms)


class ServedCommsRetiredViewsTool(ScreensArea, CoreRead[RetiredViews], CommsRetiredViewsTool):
    """Serving CommsRetiredViewsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RetiredViews:
        return comms.views.retired_views(self.threads, self.channels)


class ServedCommsThreadConfigurationTool(
    ScreensArea, CoreRead[CoordinationChangedUpdate], CommsThreadConfigurationTool
):
    """Serving CommsThreadConfigurationTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> CoordinationChangedUpdate:
        return comms.configuration.answer(self.thread)


class ServedCommsAgentSettingsTool(
    ScreensArea, CoreRead[ScopedSettingsProjection], CommsAgentSettingsTool
):
    """Serving CommsAgentSettingsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ScopedSettingsProjection:
        scope = AgentSettingsScope.parse(self.scope)
        return AgentSettings.at(comms.root).projection(scope, channel=self.channel)


class ServedCommsSetAgentSettingsTool(
    ScreensArea, HumanToolRequest[ScopedSettingsProjection], CommsSetAgentSettingsTool
):
    """Serving CommsSetAgentSettingsTool."""

    def answered(self, comms: Comms) -> ScopedSettingsProjection:
        scope = AgentSettingsScope.parse(self.scope)
        return AgentSettings.at(comms.root).update_scope(
            scope, self.values, channel=self.channel
        )


class ServedCommsSessionThreadTool(
    ScreensArea, CoreRead[ProjectThreadSelection], CommsSessionThreadTool
):
    """Serving CommsSessionThreadTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ProjectThreadSelection:
        return comms.registry.session_thread(Path(self.project), self.preferred)


class ServedCommsPinChannelTool(ChannelsArea, NativeToolRequest, CommsPinChannelTool):
    """Serving CommsPinChannelTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return PinChannelCliCommand(name=self.name, pinned=self.pinned).apply(comms)


class ServedCommsPinThreadTool(ChannelsArea, NativeToolRequest, CommsPinThreadTool):
    """Serving CommsPinThreadTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelView:
        PinThreadCliCommand(channel=self.channel, name=self.name, pinned=self.pinned).apply(comms)
        return comms.views.channel_view(ChannelTarget.normalize_name(self.channel))


class ServedCommsModelTool(
    ThreadsArea, ConfigurableThreadCommand, NativeToolRequest, CommsModelTool
):
    """Serving CommsModelTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ModelChanged:
        """Change this thread's model or switch execution mode for future turns."""
        thread_name = self.thread.name(caller)
        original = comms.registry.require(thread_name)
        thread = self.model.apply_to_thread(comms.threads, original, self)
        return ModelChanged(
            thread=thread.name,
            model=thread.catalog_model(UndeclaredModel()),
            thinking_level=thread.thinking_level,
            service_tier=thread.service_tier,
        )


class ServedCommsSortChannelsTool(ChannelsArea, NativeToolRequest, CommsSortChannelsTool):
    """Serving CommsSortChannelsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelOrderSet:
        return ChannelOrderSet(comms.channels.set_channel_order(self.order))


class ServedCommsTagsTool(ChannelsArea, NativeToolRequest, CommsTagsTool):
    """Serving CommsTagsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> TagList:
        tags = self.edit.apply(comms.channels)
        return TagList(tuple(sorted(tags)))


class ServedCommsThreadTagsTool(ThreadsArea, NativeToolRequest, CommsThreadTagsTool):
    """Serving CommsThreadTagsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadTags:
        thread = comms.channels.update_tags(
            caller.subject(self.thread),
            add=_tag_set(self.add),
            remove=_tag_set(self.remove),
        )
        return ThreadTags(thread.name, tuple(sorted(thread.tags)))


class ServedCommsConversationPageTool(
    ScreensArea, CoreRead[MessagePage], CommsConversationPageTool
):
    """Serving CommsConversationPageTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> MessagePage:
        history = self.conversation.display_history(comms.views, self.project)
        return history.page(comms.views.presentation, self.bounds)


class ServedCommsConversationRevisionTool(
    ScreensArea, CoreRead[ConversationRevision], CommsConversationRevisionTool
):
    """Serving CommsConversationRevisionTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ConversationRevision:
        history = self.conversation.display_history(comms.views, self.project)
        return history.revision(comms.views, self.conversation, self.seen, worktree=self.project)


class ServedCommsLatestSequenceTool(
    ScreensArea, CoreRead[LatestSequence], CommsLatestSequenceTool
):
    """Serving CommsLatestSequenceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> LatestSequence:
        return LatestSequence(LiveCursor(comms.views.latest_sequence()))


class ServedCommsServedRootTool(ScreensArea, CoreRead[ServedRoot], CommsServedRootTool):
    """Serving CommsServedRootTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ServedRoot:
        root = comms.root.resolve()
        return ServedRoot(str(root), BusIdentityStore.serving(root))


class ServedCommsSetChannelMetadataTool(
    ChannelsArea, NativeToolRequest, CommsSetChannelMetadataTool
):
    """Serving CommsSetChannelMetadataTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        parent = self.parent.strip() or None
        return comms.channels.set_channel_metadata(
            self.name, parent=parent, archived=self.archived
        )


class ServedCommsSetViewTool(ChannelsArea, NativeToolRequest, CommsSetViewTool):
    """Serving CommsSetViewTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> SavedView:
        return comms.channels.set_saved_view(
            self.kind(self.name, ViewPredicate(self.match, _tag_set(self.tags)))
        )


class ServedCommsDeleteViewTool(ChannelsArea, NativeToolRequest, CommsDeleteViewTool):
    """Serving CommsDeleteViewTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        DeleteViewCliCommand(name=self.name).apply(comms)
        return channels.CommsChannelsTool().apply(comms, caller)


class ServedCommsSortChannelTool(ChannelsArea, NativeToolRequest, CommsSortChannelTool):
    """Serving CommsSortChannelTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Channel:
        return comms.channels.set_channel_sort(self.name, self.order)


class ServedCommsSetProjectTool(ThreadsArea, ThreadToolRequest, CommsSetProjectTool):
    """Serving CommsSetProjectTool."""

    def apply(self, comms: Comms, caller: ThreadCaller) -> ProjectSet:
        result = comms.threads.set_project(caller.identity(comms), self.path)
        return ProjectSet(
            previous=result.previous,
            current=result.current,
            changed=result.changed,
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


class ServedCommsThreadsTool(ThreadsArea, ShortListed, ReadTool, NativeToolRequest, CommsThreadsTool):
    """Serving CommsThreadsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadListing:
        threads = ThreadsCliCommand(active_only=self.active_only).apply(comms)
        return caller.listing(comms, threads.threads)


class ServedCommsRegistryTool(ThreadsArea, CoreRead[RegistrySnapshot], CommsRegistryTool):
    """Serving CommsRegistryTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RegistrySnapshot:
        return comms.registry.snapshot()


class ServedCommsRegistrySliceTool(ScreensArea, CoreRead[RegistrySlice], CommsRegistrySliceTool):
    """Serving CommsRegistrySliceTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RegistrySlice:
        return comms.registry.snapshot().only(self.threads)


class ServedCommsReferenceNamesTool(
    ScreensArea, CoreRead[ReferenceNames], CommsReferenceNamesTool
):
    """Serving CommsReferenceNamesTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ReferenceNames:
        return ReferenceNames.of(
            comms.registry.snapshot(), comms.channels.catalog.read().channel_names()
        )


class ServedCommsMessageOutcomesTool(
    MessagesArea, ReadTool, NativeToolRequest, CommsMessageOutcomesTool
):
    """Serving CommsMessageOutcomesTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> MessageOutcomes:
        source, = comms.bus.bus.deliveries_for_references((self.reference,))
        if not caller.outcome_viewer(comms.registry).sees(source):
            raise ToolRefusedError(f"Message {source.message.seq} is not readable by this caller.")
        window = LiveNotificationRead(
            (self.reference,), after=self.after, limit=self.limit
        ).apply(comms, caller)
        page = window.outcomes.get((source.message.seq, source.message.message_id), ())
        return MessageOutcomes(
            source.message,
            page,
            window.remaining(self.reference),
            AfterRecipient(page[-1].recipient_identity) if page else self.after,
        )


class ServedCommsResourcesTool(HostArea, ThreadReadTool, CommsResourcesTool):
    """Serving CommsResourcesTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ToolResult:
        return HostResources.read(comms.root, comms.registry.require(caller.subject(self.thread)))


class ServedCommsHostLogTool(HostArea, ReadTool, NativeToolRequest, CommsHostLogTool):
    """Serving CommsHostLogTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> HostLogRead:
        if not 1 <= self.lines <= self.longest:
            raise ValueError(f"lines must be from 1 to {self.longest}, not {self.lines}")
        return HostLogRead(self.source.read(comms.root, self.lines))


class ServedCommsWorkBranchesTool(WorkArea, ReadTool, NativeToolRequest, CommsWorkBranchesTool):
    """Serving CommsWorkBranchesTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> WorkBranchList:
        branches = WorkBranches(Path(self.checkout)).read(comms.registry.snapshot())
        return WorkBranchList(
            tuple(branch for branch in branches if not (self.unlinked_only and branch.linked))
        )


class ServedCommsCollaborationTool(ThreadsArea, ThreadToolRequest, CommsCollaborationTool):
    """Serving CommsCollaborationTool."""

    def apply(self, comms: Comms, caller: ThreadCaller) -> CollaborationEdited:
        return CollaborationEdited(
            comms.relationships.edit(caller.name, self.action, self.peer, self.note)
        )


class ServedCommsCollaborationsTool(ThreadsArea, ThreadReadTool, CommsCollaborationsTool):
    """Serving CommsCollaborationsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> CollaborationList:
        contacts = comms.relationships.contacts(caller.subject(self.thread))
        visible = (
            tuple(
                VisibleCollaborator(
                    entry.target,
                    entry.available,
                    entry.sources,
                    entry.detail,
                    entry.goal_contacts,
                )
                for entry in contacts.visible
            )
            if any(entry.goal_contacts for entry in contacts.visible)
            else ()
        )
        return CollaborationList(contacts.explicit, visible, contacts.diagnostics)


class ServedCommsRenameSelfTool(ThreadsArea, ThreadToolRequest, CommsRenameSelfTool):
    """Serving CommsRenameSelfTool."""

    def apply(self, comms: Comms, caller: ThreadCaller) -> RenameThreadResult:
        return comms.threads.rename_self(self.display_name, caller=caller)


class ServedCommsSetOwnThreadTitleTool(ThreadsArea, ThreadToolRequest, CommsSetOwnThreadTitleTool):
    """Serving CommsSetOwnThreadTitleTool."""

    def apply(self, comms: Comms, caller: ThreadCaller) -> ThreadTitleResult:
        return comms.threads.set_own_title(self.title, caller=caller)


class ServedCommsSendRefusalsTool(MessagesArea, ReadTool, NativeToolRequest, CommsSendRefusalsTool):
    """Serving CommsSendRefusalsTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> SendRefusalsRead:
        return SendRefusalsRead(
            SendRefusal.recent(
                comms.root, sender=self.sender, target=self.target, limit=self.limit
            )
        )


class ServedCommsUserSendTool(
    ScreensArea, PublishingToolRequest[ToolCaller, MessagePublished], CommsUserSendTool
):
    """Serving CommsUserSendTool."""

    def publish(self, comms: Comms, caller: ToolCaller) -> MessagePublished:
        caller.require_human()
        message = comms.messaging.send_user_message(
            CommandTarget.named(self.target),
            self.body,
            worktree=Checkout(comms.root),
            files=self.files,
        )
        return MessagePublished.from_send(SendResult(message, CorrectionComplete()))


class ServedMessageFileRead(MessageFileRead):
    """Files are read through their messages, by the rule that reads the messages: the
    conversation's display scope, captured for this caller, must include every one."""

    def readable(
        self,
        comms: Comms,
        caller: ToolCaller,
        references: tuple[MessageReference, ...],
    ) -> tuple[MessageReference, ...]:
        target = CommandTarget.named(self.conversation)
        scope = target.history_view(
            caller.history_viewer(self.participant),
            comms.bus.require_channel,
            ChannelHistory,
            DMHistory,
        ).capture(comms.registry.snapshot())
        found = {
            message.reference: message
            for message in comms.bus.bus.messages_for_references(references)
        }
        refused = tuple(
            reference.name
            for reference in references
            if reference not in found or not scope.includes(found[reference])
        )
        if refused:
            raise ToolRefusedError(
                f"Not in {self.conversation} as you may read it: {', '.join(refused)}"
            )
        return references


class ServedCommsMessageFilesTool(
    MessagesArea,
    ServedMessageFileRead,
    CoreRead[MessageFilesRead],
    CommsMessageFilesTool,
):
    """Serving CommsMessageFilesTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> MessageFilesRead:
        return MessageFilesRead(
            comms.messaging.files.of(self.readable(comms, caller, self.messages))
        )


class ServedCommsMessageFileTool(
    MessagesArea,
    ServedMessageFileRead,
    CoreRead[MessageFileContent],
    CommsMessageFileTool,
):
    """Serving CommsMessageFileTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> MessageFileContent:
        (message,) = self.readable(comms, caller, (self.message,))
        files = comms.messaging.files
        return files.content(files.require(message, self.position))


class ServedCommsMarkThreadViewReadTool(
    ScreensArea, HumanToolRequest[ThreadViewMarked], CommsMarkThreadViewReadTool
):
    """Serving CommsMarkThreadViewReadTool."""

    def answered(self, comms: Comms) -> ThreadViewMarked:
        comms.views.mark_thread_view_read(
            self.thread, worktree=Checkout(comms.root), through=self.through
        )
        return ThreadViewMarked(self.thread)


class CommsAuthoredTaskTool(
    ThreadToolRequest,
    PublishingToolRequest[ThreadCaller, TaskPublished],
    AuthoredTaskRequest,
    declared_name=None,
):
    """One original authorized publication path for authored task declarations."""

    @abstractmethod
    def declaration(self, owner): ...

    @abstractmethod
    def original_body(self, declaration): ...

    def publish(self, comms: Comms, caller: ThreadCaller) -> TaskPublished:
        owner = comms.registry.require(caller.name)
        declaration = self.declaration(owner)
        result = comms.messaging.send(
            owner.name,
            self.target,
            self.original_body(declaration),
            role=NoticeRole(),
            task=declaration,
        )
        return TaskPublished(result.message.reference, result.message.task, result.correction)


class ServedCommsDecisionTool(GoalsArea, CommsAuthoredTaskTool, CommsDecisionTool):
    """Serving CommsDecisionTool."""

    def declaration(self, owner):
        return Decision.from_claim(
            owner, self.scope, self.change, chosen=self.chosen, rejected=self.rejected
        )

    def original_body(self, declaration):
        return declaration.text


class ServedCommsConstraintTool(GoalsArea, CommsAuthoredTaskTool, CommsConstraintTool):
    """Serving CommsConstraintTool."""

    def declaration(self, owner):
        return Constraint.from_claim(owner, self.scope, self.change)

    def original_body(self, declaration):
        if not self.text.strip():
            raise ToolRefusedError("An authored constraint requires exact nonempty wording")
        return self.text


class ServedCommsSubtaskTool(GoalsArea, CommsAuthoredTaskTool, CommsSubtaskTool):
    """Serving CommsSubtaskTool."""

    def declaration(self, owner):
        if not self.text.strip():
            raise ToolRefusedError("A subtask observation requires its exact description")
        return Subtask.from_claim(owner, self.scope, self.change, completed=self.completed)

    def original_body(self, declaration):
        return self.text


class ServedCommsHistoryTool(
    MessagesArea, ShortListed, ReadTool, NativeToolRequest, CommsHistoryTool
):
    """Serving CommsHistoryTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> HistoryPageResult:
        target = CommandTarget.named(self.peer)
        return HistoryCliCommand(
            target=target.canonical_name(lambda name: comms.registry.require(name).name),
            me=caller.history_viewer(self.participant),
            limit=self.limit,
            before=self.before,
            sender=self.sender,
        ).apply(comms)


class ServedCommsRestartQueueTool(HostArea, ReadTool, NativeToolRequest, CommsRestartQueueTool):
    """Serving CommsRestartQueueTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartAttempts:
        return RestartAttempts(
            tuple(
                RestartQueue(comms.root).history(
                    comms.registry.require(self.name).incarnation.require_recorded()
                )
            )
        )


class ServedCommsCancelRestartTool(HostArea, NativeToolRequest, CommsCancelRestartTool):
    """Serving CommsCancelRestartTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartsCancelled:
        return RestartsCancelled(
            tuple(
                RestartQueue(comms.root).cancel(
                    comms.registry.require(self.name).incarnation.require_recorded()
                )
            )
        )


class ServedCommsForkTool(
    ThreadsArea, LiveOwnerTool, ForkableThreadCommand, ThreadContextAction, CommsForkTool
):
    """Serving CommsForkTool."""

    action_label = "Fork from this thread"

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadCreatedResult:
        parent = comms.registry.require(self.parent)
        return ForkCliCommand(
            name=self.name,
            parent=parent.name,
            task=self.task,
            project=parent.worktree,
            model=parent.catalog_model(UndeclaredModel()),
            thinking_level=parent.thinking_level,
            tags=self.tags,
        ).apply(comms)


class ServedCommsInstallTool(HostArea, NativeToolRequest, CommsInstallTool):
    """Serving CommsInstallTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> InstallLaunches:
        return InstallLaunches(InstallDue.launch_due(comms))


class ServedCommsStopTool(
    ThreadsArea, NativeOwnerCommand, OwnerLifecycleControl, ThreadContextAction, CommsStopTool
):
    """Serving CommsStopTool."""

    action_label = "Stop process"

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadStoppedResult:
        return StopCliCommand(name=self.name).apply(comms)


class ServedCommsCliInboxTool(
    ThreadsArea, NativeOwnerCommand, OwnerLifecycleControl, NativeToolRequest, CommsCliInboxTool
):
    """Serving CommsCliInboxTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadExecutionResult:
        return CliInboxCliCommand(name=self.name).apply(comms)


class ServedCommsNativeThreadTool(
    ThreadsArea, ExternalOwnerCommand, OwnerLifecycleControl, NativeToolRequest, CommsNativeThreadTool
):
    """Serving CommsNativeThreadTool."""

    def apply(self, comms: Comms, caller: ToolCaller) -> ThreadExecutionResult:
        return NativeThreadCliCommand(name=self.name).apply(comms)


class ServedCommsStartTool(
    ThreadsArea, LiveOwnerTool, NativeOwnerCommand, StartOwnerControl, ThreadContextAction, CommsStartTool
):
    """Serving CommsStartTool."""

    action_label = "Start thread"

    def apply(self, comms: Comms, caller: ToolCaller) -> OwnerStartResult:
        return StartCliCommand(name=self.name).apply(comms)


class ServedCommsQueueRestartTool(
    HostArea,
    LiveOwnerTool,
    NativeOwnerCommand,
    OwnerLifecycleControl,
    ThreadContextAction,
    CommsQueueRestartTool,
):
    """Serving CommsQueueRestartTool."""

    action_label = "Queue idle restart"

    def apply(self, comms: Comms, caller: ToolCaller) -> RestartQueued:
        return QueueRestartCliCommand(name=self.name).apply(comms)


class ServedCommsArchiveTool(
    ThreadsArea, ArchiveOwnerControl, ThreadContextAction, CommsArchiveTool
):
    """Serving CommsArchiveTool."""

    action_label = "Archive stopped thread"

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
