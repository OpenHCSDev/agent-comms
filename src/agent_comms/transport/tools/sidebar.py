"""The requests a display sends to Core: their fields, wire names and results. Serving them
is in agent_comms.transport.tools.served, which only the server imports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated

from pydantic import JsonValue

from agent_comms.acp_extension import CoordinationChangedUpdate
from agent_comms.cli_bridges.codex_records import CodexRolloutProvenance
from agent_comms.cli_verbs.cli_command import (
    PreparedTargetAction,
    TargetAction,
    TargetEdit,
)
from agent_comms.cli_verbs.cli_result import TargetEditResult
from agent_comms.context_store import (
    LATEST_CONTEXT,
    CapturedContext,
    ContextManifestCursor,
    ContextManifestPage,
    ContextManifestReference,
)
from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_origin import HumanInputOrigin
from agent_comms.execution.session_revision import SessionObservation
from agent_comms.execution.transcript_sources import TranscriptCursor
from agent_comms.execution.turn_context import (
    ContextSourceText,
    Provenance,
    SegmentManifest,
)
from agent_comms.history_views import RetiredViews
from agent_comms.importing import ThreadImported
from agent_comms.installer.harness_install import Harness
from agent_comms.messaging.channel_targets import (
    ChannelSelection,
    CommandTarget,
    NamedTarget,
    NoChannel,
)
from agent_comms.messaging.channels import Channel
from agent_comms.messaging.display_references import ReferenceNames
from agent_comms.messaging.exporting import (
    WireExportFormat,
    WireExportLimit,
    WireExportProvenance,
    WireExportScope,
)
from agent_comms.messaging.historical_views import (
    AllHistoricalSenders,
    ConversationObservation,
    ConversationRevision,
    HistoricalSenders,
    HistoricalThread,
    UnobservedConversation,
)
from agent_comms.messaging.message_page import NEWEST_PAGE, MessagePage, PageBounds
from agent_comms.messaging.page_traversal import LatestSequence
from agent_comms.messaging.send_result import MessagePublished
from agent_comms.messaging.served_bus import ServedBus, UnidentifiedBus
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AbsolutePathText, ThreadLookup, WireName
from agent_comms.presentation import WireRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import (
    ProjectThreadSelection,
    RegistryEntry,
    RegistrySlice,
    RegistrySnapshot,
)
from agent_comms.registry.relationships import (
    SortableRelationshipGroup,
    ThreadCommsSnapshot,
)
from agent_comms.registry.thread_identity import ClaimedIdentity, ThreadIncarnation
from agent_comms.registry.thread_management import (
    ImportDirectory,
    ProjectChangeResult,
    RenameThreadResult,
)
from agent_comms.transcript.transcripts import NotifiedThreadPresentation
from agent_comms.registry.thread_text import ExpectedSession
from agent_comms.registry.threads import Thread
from agent_comms.transport.request import (
    ObservedRequest,
    ReadRequest,
    ToolRequest,
    tool_field,
)
from agent_comms.transport.runtime_request import RuntimeRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.working_memory.working_memory_labels import ModelLabel


@dataclass(frozen=True, kw_only=True)
class SavedManifestRequest[R: ToolResult = ToolResult](ReadRequest[R], declared_name=None):
    """A read of one retained context manifest of a stopped thread."""

    owner: Annotated[str, *tool_field("Thread whose retained manifest to read")]
    reference: Annotated[ContextManifestReference, *tool_field("The retained manifest")]
    position: Annotated[int, *tool_field("Segment position")]


@dataclass(frozen=True)
class ChannelOrderSet(ToolResult):
    order: type[ChannelSort]


@dataclass(frozen=True)
class ContextInspectionRead(ToolResult):
    """A server registry entry keeps its session binding across transport."""

    registry_root: Annotated[Path, AbsolutePathText]
    entry: RegistryEntry
    page: ContextManifestPage
    source: SessionObservation
    annotations: tuple[ModelLabel, ...]
    imported: tuple[CodexRolloutProvenance, ...]

    def __post_init__(self) -> None:
        thread = self.entry.thread
        session = thread.session_file.bound_to(self.registry_root)
        if session is not thread.session_file:
            object.__setattr__(self, "entry", replace(
                self.entry, thread=replace(thread, session_file=session)))


@dataclass(frozen=True)
class HistoricalThreadsRead(ToolResult):
    threads: tuple[HistoricalThread, ...]


@dataclass(frozen=True)
class OwnerAnswered(ToolResult):
    payload: JsonValue


@dataclass(frozen=True)
class PageMarked(ToolResult):
    messages: int


@dataclass(frozen=True)
class QueueActionApplied(ToolResult):
    owner: str


@dataclass(frozen=True)
class RelationshipOrdered(ToolResult):
    order: type[ThreadSort]


@dataclass(frozen=True)
class ServedRoot(ToolResult):
    """Which bus a Core serves: the key its observation requests and views are read under,
    and the identity that says whose bus that key belongs to.

    The root alone was the whole answer until 2026-11-07, when a reader holding two
    connectors resolved one machine's threads as the other's: every machine in this fleet
    keeps its bus at the same path, so the key matched and the bus did not. `served_bus.py`
    carries that story. A Core older than the identity answers no member at all, which is
    an answer a reader must not resolve rather than one it can compare.
    """

    root: str
    bus: ServedBus = UnidentifiedBus()


@dataclass(frozen=True)
class TargetActionsRead(ToolResult):
    actions: tuple[TargetAction, ...]


@dataclass(frozen=True)
class ThreadViewMarked(ToolResult):
    """The thread whose displayed position the owner acknowledged."""

    thread: str


@dataclass(frozen=True, kw_only=True)
class CommsCaptureHumanInputTool(ToolRequest[HumanInputOrigin]):
    """The original human author witness for input the owner is about to queue, captured
    against the claimed identity on Core's host."""

    label = "Capture human input"
    description = "Capture the human author witness for queued input; requires a human UI caller."
    claimed_identity: Annotated[ClaimedIdentity, *tool_field("The claimed input identity")]


@dataclass(frozen=True, kw_only=True)
class CommsChannelTool(ReadRequest[Channel]):
    """One channel as Core declares it; a UI asks it whether the channel takes input."""

    label = "Channel"
    description = "One channel's declaration: its kind, members and whether it is read-only."
    name: Annotated[str, *tool_field("Channel name")]


@dataclass(frozen=True, kw_only=True)
class CommsContextInspectionTool(ReadRequest[ContextInspectionRead]):
    """One thread's recorded context page, with its annotations and imported sources, as a
    context explorer shows it."""

    label = "Context inspection"
    description = (
        "One thread's recorded context manifests, working-memory annotations and imported sources."
    )
    owner: Annotated[str, *tool_field("Thread whose context to read")]
    cursor: Annotated[ContextManifestCursor, *tool_field("Where the page starts")] = LATEST_CONTEXT
    previous: Annotated[
        tuple[CapturedContext, ...], *tool_field("Contexts the reader already holds")
    ] = ()


@dataclass(frozen=True, kw_only=True)
class CommsConversationPageTool(ReadRequest[MessagePage]):
    """The UI's conversation read: the same request locally and through the connector."""

    label = "Conversation page"
    description = "Read one bounded display page of a channel or a DM as the owner's UI shows it."
    conversation: Annotated[NamedTarget, *tool_field("The channel or the DM's thread")]
    project: Annotated[Checkout, *tool_field("The project whose human user reads the page")]
    bounds: Annotated[PageBounds, *tool_field("Which page: direction, size; default newest")] = (
        NEWEST_PAGE
    )


@dataclass(frozen=True, kw_only=True)
class CommsConversationRevisionTool(ReadRequest[ConversationRevision]):
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


@dataclass(frozen=True, kw_only=True)
class CommsExportWireTool(ObservedRequest[WireExportProvenance]):
    """A UI exports wire history to a file on Core's host, with progress and cancellation.
    Requires a human caller."""

    label = "Export wire"
    description = (
        "Export wire history to a file on Core's host, reporting progress and stopping "
        "when cancelled; requires a human UI caller."
    )
    destination: Annotated[Path, *tool_field("Destination file on Core's host")]
    format: Annotated[WireExportFormat, *tool_field("Export format")]
    scope: Annotated[WireExportScope, *tool_field("What to export")]
    limit: Annotated[WireExportLimit, *tool_field("How much to export")]


@dataclass(frozen=True, kw_only=True)
class CommsHistoricalThreadsTool(ReadRequest[HistoricalThreadsRead]):
    """The preserved identities a UI's saved-sessions view lists, for every sender or one."""

    label = "Historical threads"
    description = (
        "Preserved thread identities from attached history sources, for every sender or one name."
    )
    senders: Annotated[HistoricalSenders, *tool_field("Every sender, or one name")] = (
        AllHistoricalSenders()
    )


@dataclass(frozen=True, kw_only=True)
class CommsImportThreadTool(ToolRequest[ThreadImported]):
    """A UI imports a harness session as a stopped thread; Core's host reads the source file.
    Requires a human caller."""

    label = "Import thread"
    description = (
        "Import a harness session file on Core's host as a stopped thread; requires a "
        "human UI caller."
    )
    source: Annotated[Path, *tool_field("Session file on Core's host")]
    harness: Annotated[type[Harness], *tool_field("Harness that wrote the session")]
    name: Annotated[str, *tool_field("Thread name")]
    session: Annotated[ExpectedSession, *tool_field("Expected session")]
    worktree: Annotated[ImportDirectory, *tool_field("Worktree for the imported thread")]


@dataclass(frozen=True, kw_only=True)
class CommsImportedSourceTool(ReadRequest[ContextSourceText]):
    """The public text of one source an imported thread's snapshot retained."""

    label = "Imported source"
    description = "The public text of one source in an imported thread's original snapshot."
    owner: Annotated[Thread, *tool_field("The imported thread as the reader saw it")]
    source: Annotated[Provenance, *tool_field("The retained source")]


@dataclass(frozen=True, kw_only=True)
class CommsLatestSequenceTool(ReadRequest[LatestSequence]):
    """The bound a UI reads new messages up to, locally or through the connector."""

    label = "Latest sequence"
    description = "The sequence number of the newest message on the bus, whoever it was sent to."


@dataclass(frozen=True, kw_only=True)
class CommsMarkPageReadTool(ToolRequest[PageMarked]):
    """A UI acknowledges exactly the page it painted, as the human user of its project."""

    label = "Mark page read"
    description = (
        "Acknowledge the read evidence of exactly one painted conversation page, as "
        "the project's human user; requires a human UI caller."
    )
    page: Annotated[MessagePage, *tool_field("The painted page")]
    worktree: Annotated[Checkout, *tool_field("The project whose human user read it")]


@dataclass(frozen=True, kw_only=True)
class CommsMarkThreadViewReadTool(ToolRequest[ThreadViewMarked]):
    """A UI acknowledges what it displayed of one thread, as the owner, locally or across
    the connector; requires a human UI caller."""

    label = "Mark thread view read"
    description = (
        "Acknowledge the transcript position a UI displayed for one thread, through "
        "the thread's receiving contract, as the owner; requires a human UI caller."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    through: Annotated[TranscriptCursor, *tool_field("Displayed transcript position")]


@dataclass(frozen=True, kw_only=True)
class CommsMoveThreadProjectTool(ToolRequest[ProjectChangeResult]):
    """The owner's UI moves one thread incarnation's project; a later occupant of the name
    is refused, never moved."""

    label = "Move thread project"
    description = "Move a thread incarnation's project directory; requires a human UI caller."
    owner: Annotated[ThreadIncarnation, *tool_field("The thread incarnation the UI holds")]
    path: Annotated[str, *tool_field("New project directory")]


@dataclass(frozen=True, kw_only=True)
class CommsOwnerRequestTool(ToolRequest[OwnerAnswered]):
    """A UI asks one running owner incarnation a declared runtime request, such as its
    context; Core's host reaches the owner's socket. Requires a human caller."""

    label = "Owner request"
    description = (
        "Send a declared runtime request to one running thread incarnation's owner "
        "and return its reply; requires a human UI caller."
    )
    owner: Annotated[ThreadIncarnation, *tool_field("The owner incarnation the UI holds")]
    request: Annotated[RuntimeRequest, *tool_field("The declared runtime request")]


@dataclass(frozen=True, kw_only=True)
class CommsPrepareTargetActionTool(ReadRequest[PreparedTargetAction]):
    """Opening a menu action, or changing its values, asks Core which choices it offers."""

    label = "Prepare target action"
    description = (
        "The choices Core's configuration offers a menu action's fields, such as the "
        "thinking levels of its chosen model."
    )
    action: Annotated[TargetAction, *tool_field("The action with its current values")]


class QueueAction(DeclaredFamily, affix="QueueAction"):
    """What a UI asks a thread's owner to do with its queued inputs. Core serves each
    member with the owner request that declares it serves it."""


class SendNowQueueAction(QueueAction, declared_name="send_now"):
    """Send the queued message now."""


class ClearQueueQueueAction(QueueAction, declared_name="clear_queue"):
    """Clear the queue."""


@dataclass(frozen=True, kw_only=True)
class CommsQueueActionTool(ToolRequest[QueueActionApplied]):
    """A UI sends a queued message now, or clears a queue, at its original recipient's owner;
    Core's host reaches the owner. Requires a human caller."""

    label = "Queue action"
    description = (
        "Send a queued message now, or clear the queue, at the original recipient's "
        "running owner; requires a human UI caller."
    )
    lookup: Annotated[ThreadLookup, *tool_field("The original recipient's creation lookup")]
    action: Annotated[type[QueueAction], *tool_field("Send now or clear")]


@dataclass(frozen=True, kw_only=True)
class CommsReferenceNamesTool(ReadRequest[ReferenceNames]):
    """A UI resolves @thread and #channel links without the registry or channel activity."""

    label = "Reference names"
    description = (
        "Every linkable thread name with its incarnation, and every channel name, without thread"
        " records."
    )


@dataclass(frozen=True, kw_only=True)
class CommsRegistrySliceTool(ReadRequest[RegistrySlice]):
    """A remote UI reads the registry facts of the threads it is showing, not every thread's."""

    label = "Thread registry slice"
    description = (
        "The thread registry snapshot holding only the named threads: their rows, "
        "statuses, aliases and owner generations, with the fields every thread shares."
    )
    threads: Annotated[tuple[str, ...], *tool_field("Thread names, current or renamed")]


@dataclass(frozen=True, kw_only=True)
class CommsRegistryTool(ReadRequest[RegistrySnapshot]):
    """A remote UI resolves names, renames and recorded threads against Core's registry."""

    label = "Thread registry"
    description = "The thread registry snapshot: names, aliases, statuses and owner generations."


@dataclass(frozen=True, kw_only=True)
class CommsRenameManagedThreadTool(ToolRequest[RenameThreadResult]):
    """The owner's UI renames the running thread its agent process manages."""

    label = "Rename managed thread"
    description = (
        "Rename a running managed thread after proving its process; requires a human UI caller."
    )
    thread: Annotated[ThreadIncarnation, *tool_field("The thread incarnation the UI holds")]
    display_name: Annotated[str, *tool_field("The new display name")]
    owner: Annotated[ProcessIdentity, *tool_field("The process that manages the thread")]


@dataclass(frozen=True, kw_only=True)
class CommsRetiredViewsTool(ReadRequest[RetiredViews]):
    """A UI asks which of its open thread and channel views lost their source."""

    label = "Retired views"
    description = (
        "Which of these thread incarnations and channel views were deleted, replaced or archived."
    )
    threads: Annotated[frozenset[ThreadIncarnation], *tool_field("Open thread views")]
    channels: Annotated[frozenset[str], *tool_field("Open channel views")]


@dataclass(frozen=True, kw_only=True)
class CommsSavedSegmentTool(SavedManifestRequest[SegmentManifest]):
    """One segment of a stopped thread's retained manifest, with its selected contributors."""

    label = "Saved context segment"
    description = "One segment of a stopped thread's retained context manifest."
    contributors: Annotated[tuple[int, ...], *tool_field("Contributor positions")] = ()


@dataclass(frozen=True, kw_only=True)
class CommsSavedSourceTool(SavedManifestRequest[ContextSourceText]):
    """The public text of one source in a stopped thread's retained manifest."""

    label = "Saved context source"
    description = "The public text of one source in a stopped thread's retained context manifest."
    source: Annotated[Provenance, *tool_field("The retained source")]


@dataclass(frozen=True, kw_only=True)
class CommsServedRootTool(ReadRequest[ServedRoot]):
    """A remote UI keys its observation by the root the connector's Core serves."""

    label = "Served root"
    description = "The bus root this Core serves; a UI keys its observation requests by it."


@dataclass(frozen=True, kw_only=True)
class CommsSessionThreadTool(ReadRequest[ProjectThreadSelection]):
    """An unbound UI session finds its project's thread; Core's host resolves the path its
    registry records, so a detached copy never matches through a local alias."""

    label = "Session thread"
    description = (
        "Select the thread an unbound session at a project names: its preferred name, "
        "else the project's sole declared ACP thread."
    )
    project: Annotated[str, *tool_field("Project path as the UI names it")]
    preferred: Annotated[str, *tool_field("Preferred thread name")]


@dataclass(frozen=True, kw_only=True)
class CommsSetRelationshipOrderTool(ToolRequest[RelationshipOrdered]):
    """A UI sets how one of a thread's relationship groups is sorted; requires a human caller."""

    label = "Set relationship order"
    description = (
        "Set how one sortable relationship group of a thread is ordered; requires a human UI"
        " caller."
    )
    owner: Annotated[str, *tool_field("Thread whose group to order")]
    group: Annotated[type[SortableRelationshipGroup], *tool_field("Relationship group")]
    order: Annotated[type[ThreadSort], *tool_field("Thread order")]


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelTool(ToolRequest[Channel]):
    label = "Sort channel members"
    description = "Persist one channel's member ordering, shared by all attached views."
    name: Annotated[str, *tool_field("Channel name")]
    order: Annotated[type[ThreadSort], *tool_field("Member ordering")]


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelsTool(ToolRequest[ChannelOrderSet]):
    label = "Sort Channels"
    description = (
        "Persist channel-list ordering independently of member ordering. Pinned "
        "channels stay first, with built-in channels first within each pin group."
    )
    order: Annotated[type[ChannelSort], *tool_field("Channel list order")]


@dataclass(frozen=True, kw_only=True)
class CommsTargetActionsTool(ReadRequest[TargetActionsRead]):
    """A UI's thread or channel menu: the actions its selection offers. Opening one prepares
    its fields through CommsPrepareTargetActionTool, so listing never runs Pi."""

    label = "Target actions"
    description = "The actions a thread or channel menu offers for these targets."
    project: Annotated[Checkout, *tool_field("Project the menu was opened in")]
    targets: Annotated[tuple[CommandTarget, ...], *tool_field("Selected threads or channels")]
    channel: Annotated[ChannelSelection, *tool_field("Surrounding channel")] = NoChannel()


@dataclass(frozen=True, kw_only=True)
class CommsTargetEditTool(ToolRequest[TargetEditResult | Channel]):
    """A UI applies one menu action, as the owner, locally or across the connector."""

    label = "Apply target action"
    description = (
        "Apply a thread or channel menu action with the values its editor accepted; "
        "requires a human UI caller."
    )
    edit: Annotated[TargetEdit, *tool_field("The accepted action and its values")]


@dataclass(frozen=True, kw_only=True)
class CommsThreadConfigurationTool(ReadRequest[CoordinationChangedUpdate]):
    """A UI shows a thread's configuration from Core, in process or across the connector."""

    label = "Thread configuration"
    description = "One thread's configuration: its model, thinking level, title and worktree."
    thread: Annotated[str, *tool_field("Thread name")]


@dataclass(frozen=True, kw_only=True)
class CommsThreadPresentationTool(ReadRequest[NotifiedThreadPresentation]):
    """One thread's published presentation, as the sidebar and session header show it.

    The result is the published NotifiedThreadPresentation, with its notifications and read
    identity: a read answered across a pipe decodes as the declared type, and on 2026-11-10
    declaring the parent made every local Toad read refuse those two fields as unknown."""

    label = "Thread presentation"
    description = "Read one thread's published presentation."
    thread: Annotated[str, *tool_field("Thread name")]


@dataclass(frozen=True, kw_only=True)
class CommsThreadRelationshipsTool(ReadRequest[ThreadCommsSnapshot]):
    """One thread's relationship groups, as its sidebar shows them."""

    label = "Thread relationships"
    description = "One thread's inbound, outbound, parent, child and collaborating threads."
    owner: Annotated[str, *tool_field("Thread whose relationships to read")]


@dataclass(frozen=True, kw_only=True)
class CommsUserSendTool(ToolRequest[MessagePublished]):
    """Local and connector UI input use the existing durable human publication."""

    label = "Send human input"
    description = (
        "Publish input typed by the owner to a thread or channel under the owner's "
        "durable user identity; requires a human UI caller."
    )
    target: Annotated[str, *tool_field("Target thread name or channel", WireName("to"))]
    body: Annotated[str, *tool_field("Human input text")]


@dataclass(frozen=True, kw_only=True)
class CommsWireRevisionTool(ReadRequest[WireRevision]):
    """What every wire view derives from; an equal revision shows the same thing."""

    label = "Wire revision"
    description = "The wire revision thread, relationship and conversation views derive from."
