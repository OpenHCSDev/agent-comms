"""History, display snapshots and viewer read-state ownership."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import cached_property, partial
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.context_segments.retained import RetainedSegment
from agent_comms.coordination.errors import StaleRevision
from agent_comms.errors import UnknownChannelError
from agent_comms.execution.transcript_sources import TranscriptCursor
from agent_comms.input_disposition import InputDispositions, ScopedInputDispositions
from agent_comms.messaging.activity_clocks import ChannelActivity
from agent_comms.messaging.channel_management import ChannelManagement
from agent_comms.messaging.channel_targets import (
    ChannelTarget,
    CommandTarget,
    ThreadTarget,
)
from agent_comms.messaging.channels import ChannelMemberIndex
from agent_comms.messaging.exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportProvenance,
    WireExportScope,
    WireTranscriptExporter,
)
from agent_comms.messaging.historical_views import (
    ChannelDisplayHistory,
    ChannelHistory,
    DMDisplayHistory,
    DMHistory,
    HistorySource,
)
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_notification import (
    MessageNotification,
    NotificationWindow,
)
from agent_comms.messaging.message_page import NEWEST_PAGE, MessagePage, PageBounds
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.messaging.notification_evidence import (
    FirstRecipient,
    OutcomeViewer,
    RecipientCursor,
    RecipientLimit,
)
from agent_comms.messaging.retained_task_facts import RetainedTaskFacts
from agent_comms.messaging.sent_message import AssignedSourceIdentity
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, PresenceExclude, WireForm
from agent_comms.notification_assignment import AssignmentSource
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.notification_selection_read import ThreadSourceReader
from agent_comms.registry.activity import ActivityCapture, ObservedActivity
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import (
    DEFAULT_ROSTER,
    RegistrySnapshot,
    RosterVisibility,
)
from agent_comms.registry.shared_ledger import LedgerDocument
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import ConversationPreparation, Thread, current_thread
from agent_comms.registry.view_unread import transcript_read_state
from agent_comms.transcript.transcripts import Transcripts
from agent_comms.transport.tool_result import ToolResult

from agent_comms.registry.registry_names import DeletedThread
from agent_comms.messaging.page_traversal import LiveCursor
from agent_comms.messaging.read_basis import (
    AnonymousViewer,
    DisplayViewer,
    DMDisplayBasis,
    NamedViewer,
)
from agent_comms.messaging.recorded_bus import RecordedBusIdentity
from agent_comms.messaging.sending import Messaging
from agent_comms.planning.goal_management import Goals
from agent_comms.presentation import (
    BusPresentation,
    ChannelView,
    CoordinationSnapshot,
    CountedRead,
    InboxRead,
    NoRevision,
    ReadState,
    ThreadRecord,
    ThreadView,
    WireRevision,
)
from agent_comms.registry.collaboration_ledger import CollaborationLedger
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.relationships import RelationshipSource
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER, RosterVisibility
from agent_comms.registry.thread_presentation import (
    NoThreadPresentation,
    ThreadPresentation,
)
from agent_comms.storage.private_path import FileRevision
from agent_comms.transport.progress import WireExportObserver

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollTarget(DeclaredFamily, affix="Target"):
    """Whose poll this is: the thread that calls, or one named on the command line."""

    @abstractmethod
    def thread(self, registry: Registration) -> Thread: ...


@dataclass(frozen=True)
class CallingThreadTarget(PollTarget):
    def thread(self, registry: Registration) -> Thread:
        return registry.require(current_thread(registry).name)


@dataclass(frozen=True)
class NamedThreadTarget(PollTarget):
    name: str

    def thread(self, registry: Registration) -> Thread:
        return registry.require(self.name)

    @classmethod
    def argument(cls, value: str) -> dict[str, object]:
        return FieldCodec.encode_record(cls(value))


@dataclass(frozen=True)
class ThreadPoll(CliResult):
    """One thread's state, its inbox, the shared ledger and its peers at one read."""
    thread: ThreadRecord
    inbox: tuple[Annotated[Message, WireForm], ...]
    ledger: Annotated[LedgerDocument, WireForm]
    peers: tuple[ThreadView, ...]

    def to_wire(self) -> object:
        return FieldCodec.project(self, PresenceExclude)


@dataclass(frozen=True)
class ThreadViewRead(ConversationPreparation):
    views: HistoryViews
    thread: Thread
    viewer: Thread
    through: TranscriptCursor

    def native(self):
        self.through.mark_read(self.views.transcript_reads, self.viewer.name, self.thread.runtime)

    def external(self):
        self.through.wire.require_source(AssignedSourceIdentity(str(self.views.root), self.thread.incarnation))
        page = DMDisplayBasis.fetch_page(
            self.views.bus, self.views.registry, self.views.root, self.viewer,
            ThreadTarget(self.thread.name), worktree=self.viewer.worktree,
            bounds=PageBounds(LiveCursor(self.through.wire.sequence + 1).older()),
        )
        if page.messages:
            self.views.mark_page_read(page, worktree=self.viewer.worktree)


@dataclass(frozen=True)
class RetiredViews(ToolResult):
    """Views whose source no longer exists, as Core decided from one registry read."""

    threads: frozenset[ThreadIncarnation]
    channels: frozenset[str]


@dataclass(frozen=True)
class ThreadPresentations(AssignmentSource, ThreadSourceReader[ThreadPresentation]):
    """One status acquisition shared by a reader's thread presentations.

    These rows and their shared assignment acquisition belong to this cut,
    not a persistent cache. Transcript windows and failures remain per thread.
    """

    snapshot: RegistrySnapshot
    views: Mapping[str, ThreadView]
    transcripts: Transcripts
    sent_source_limit: int

    @property
    def registry(self) -> RegistrySnapshot:
        return self.snapshot

    @contextmanager
    def readings(self, names: Iterable[str]) -> Iterator[Mapping[str, Callable[[], ThreadPresentation]]]:
        with ExitStack() as scope:
            batch = OpenThreadPresentations(
                **{declared.name: getattr(self, declared.name) for declared in fields(self)},
                scope=scope,
            )
            yield {name: partial(batch.read, name) for name in names}

    def view(self, name: str) -> ThreadPresentation:
        canonical = self.snapshot.canonical_name(name)
        if canonical not in self.views:
            return NoThreadPresentation()
        return self.views[canonical].presentation

    def read(self, name: str) -> ThreadPresentation:
        return self.view(name).read_sources(self, self.snapshot.canonical_name(name))

    def read_source(self, name: str, ordinary: ThreadPresentation) -> ThreadPresentation:
        captured = self.transcripts.capture_page_window(
            name, source_limit=5, sent_source_limit=self.sent_source_limit,
        )
        if not captured.read.identity.thread.incarnation.current(self.snapshot):
            raise StaleRevision("Thread source incarnation changed during presentation")
        return captured.presentation(ordinary, self)

    def require(self, name: str) -> ThreadPresentation:
        """A bound caller must name an available thread, not an anonymous view."""
        return self.read(name).require(name)


@dataclass(frozen=True)
class OpenThreadPresentations(ThreadPresentations):
    """A presentation pass owns its input snapshot until all readers finish."""

    scope: ExitStack = field(kw_only=True, repr=False, compare=False)

    @cached_property
    def input_dispositions(self) -> ScopedInputDispositions:
        return ScopedInputDispositions(self.root, self.scope)


class HistoryViews(RelationshipSource):
    def __init__(
        self,
        root: Path,
        registry: Registration,
        bus: MessageBus,
        channels: ChannelManagement,
        agents: AgentActivity,
        transcripts: Transcripts,
        messaging: Messaging,
        goals: Goals,
        ledger: CollaborationLedger,
    ):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.channels = channels
        self.agents = agents
        self.transcripts = transcripts
        self.messaging = messaging
        self.goals = goals
        self.ledger = ledger
        self.presentation = BusPresentation(bus, registry, channels.catalog, agents, goals)
        self.transcript_reads = transcript_read_state(bus.reads.path)
        self.captured_activity: tuple[ReadState, ActivityCapture[ObservedActivity | DeletedThread]] = (
            NoRevision(), ActivityCapture.empty())

    def observed_activity(
        self, revision: WireRevision, names: Iterable[str],
    ) -> dict[str, ObservedActivity | DeletedThread]:
        """The named threads' activity as of one observer revision.

        Every notification request and every thread read asks; each revision captures
        a name once, so a busy bus opens the activity and room stores once per
        revision instead of once per reader. A busy row turning idle with age moves
        no revision field, so the capture also ends when its own expiry passes."""
        held, capture = self.captured_activity
        if held != revision or capture.expired():
            capture = ActivityCapture.empty()
        wanted = tuple(names)
        if missing := set(wanted) - capture.activity.keys():
            capture = capture.joined(self.registry.snapshot().observed_activity(self.agents, missing))
        self.captured_activity = (revision, capture)
        return {name: capture.activity[name] for name in wanted if name in capture.activity}

    def message_notifications_for_references(
        self,
        viewer: OutcomeViewer,
        references: Sequence[MessageReference],
        *,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
    ) -> NotificationWindow:
        return MessageNotification.references(
            self.root,
            self.registry,
            self.bus.bus,
            references,
            viewer=viewer,
            after=after,
            limit=limit,
        )

    def history_page(self, target: CommandTarget, bounds: PageBounds = NEWEST_PAGE,
                     *, viewer: DisplayViewer = AnonymousViewer()) -> MessagePage:
        """The decoded target selects its scope and participant requirements."""
        view = target.history_view(viewer, self.bus.require_channel, ChannelHistory, DMHistory)
        return view.page(self.presentation, bounds)

    def attach_history(self, source_root: Path, *, source_read: HistorySource | None = None) -> HistorySource:
        """Attach preserved history without allowing any historical execution."""
        source = self.bus.history.attach(Path(source_root), source_read=source_read)
        catalog = ChannelCatalog.at(source.root)
        incoming = catalog.read()
        with self.bus.opened_activity() as activity:
            self.channels.catalog.restore_missing(incoming, activity)
        return source

    def dm_display_page(
        self, peer: ThreadTarget, *, worktree: Checkout, bounds: PageBounds = NEWEST_PAGE
    ) -> MessagePage:
        return self.dm_history(peer, worktree=worktree).page(self.presentation, bounds)

    def dm_history(self, peer: ThreadTarget, *, worktree: Checkout) -> DMDisplayHistory:
        """A DM as the human user of `worktree` reads it."""
        return DMDisplayHistory(self.messaging.user_identity(worktree).name, peer, worktree)

    def channel_history(
        self, channel: ChannelTarget, *, worktree: Checkout
    ) -> ChannelDisplayHistory:
        """A channel as the human user of `worktree` reads it."""
        return ChannelDisplayHistory(
            self.bus.require_channel(channel.name), self.display_viewer(worktree))

    def display_viewer(self, worktree: Checkout) -> NamedViewer:
        """Select the human reader; page capture resolves it in its own snapshot."""
        return NamedViewer(self.messaging.user_identity(worktree).name)

    def channel_display_page(
        self,
        target: str,
        *,
        viewer: DisplayViewer = AnonymousViewer(),
        bounds: PageBounds = NEWEST_PAGE,
    ) -> MessagePage:
        channel = self.bus.require_channel(target)
        return ChannelDisplayHistory(channel, viewer).page(self.presentation, bounds)

    def mark_page_read(self, page: MessagePage, *, worktree: Checkout) -> None:
        """Acknowledge the original read evidence of exactly the painted page."""
        page.selection.acknowledge(self, page, worktree=worktree)

    def last_sent_timestamps(
        self, rows: Iterable[tuple[str, float]], *, registry: RegistrySnapshot,
    ) -> dict[str, float]:
        """Map each supplied sender clock through its current registry name."""
        canonical: dict[str, float] = {}
        for sender, timestamp in rows:
            name = registry.canonical_name(sender)
            canonical[name] = max(canonical.get(name, 0.0), timestamp)
        return canonical

    def retained_context(self, name: str) -> RetainedSegment:
        """Project authored context and its export boundary from one captured bus read."""
        snapshot = self.registry.snapshot()
        owner = snapshot.require(name)
        with self.bus.bus.reading() as source:
            facts = RetainedTaskFacts.capture(source, owner.incarnation)
            retained = RetainedTaskFacts((
                *facts.facts, *facts.original_input_facts(InputDispositions(self.root)),
            ))
            boundary = WireExportBoundary(source.revision(), time.time())
        return RetainedSegment.capture(
            retained.for_owner(owner, snapshot), owner, snapshot, boundary,
        )

    def export_wire(
        self,
        destination: Path | str,
        *,
        format: WireExportFormat,
        scope: WireExportScope,
        limit: WireExportLimit,
        overwrite: bool = False,
        export_started_at: float | None = None,
        observer: WireExportObserver = WireExportObserver(),
    ) -> WireExportProvenance:
        """Export one fixed, non-mutating snapshot of authoritative wire rows."""
        started_at = time.time() if export_started_at is None else export_started_at
        resolved = scope.resolve(self.bus.require_channel, self.registry)
        with self.bus.bus.reading() as source:
            through = source.revision()
            with source.stored_message_history(
                resolved.predicate, resolved.parameters, through=through,
            ) as selected:
                return WireTranscriptExporter(
                    format=format,
                    scope=resolved.scope,
                    limit=limit,
                    boundary=WireExportBoundary(through, started_at),
                ).export_stored(selected, Path(destination).expanduser(), overwrite=overwrite,
                                observer=observer)

    def channel_views(
        self, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> tuple[ChannelView, ...]:
        """Channel declarations and current members; clients never infer membership."""
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        with self.bus.opened_activity() as clocks:
            channels = catalog.views(clocks)
            return ChannelView.roster(
                snapshot,
                channels.visible(visibility),
                catalog.pinned_members(),
                catalog.list_order,
                self.agents.all_activity(
                    snapshot.visible_threads(visibility), snapshot=snapshot,
                ),
                self.last_sent_timestamps(clocks.sent.items(), registry=snapshot),
                channels.history_activity(clocks.channels),
                visibility,
            )

    def channel_view(self, name: str, visibility: RosterVisibility = DEFAULT_ROSTER) -> ChannelView:
        """Read one channel; observe and order only its own members."""
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        with self.bus.opened_activity() as clocks:
            channel = catalog.require_channel(name, clocks)
            registered = channel.members(ChannelMemberIndex(snapshot.threads.values()))
            rows = ((alias, clocks.sent[alias]) for thread in registered
                    for alias in snapshot.names_for(thread.name) if alias in clocks.sent)
            sent = self.last_sent_timestamps(rows, registry=snapshot)
            history = channel.history_activity(clocks.channels)
            return ChannelView.capture(
                channel, registered, snapshot, catalog.pinned_threads(channel.name),
                self.agents.all_activity(registered, snapshot=snapshot), sent, history, visibility,
            )

    def thread_views(
        self, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> tuple[ThreadView, ...]:
        snapshot = self.registry.snapshot()
        return ThreadView.roster(snapshot, self.agents, self.goals, visibility)

    def retired_views(self, threads: Iterable[ThreadIncarnation], channels: Iterable[str]) -> RetiredViews:
        """Which of these thread incarnations and channel views no longer exist.

        A thread is retired when it was deleted or replaced by a newer
        incarnation; a channel when it is gone from the catalog or archived.
        """
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        retired = set()
        with self.bus.opened_activity() as activity:
            for name in channels:
                try:
                    channel = catalog.require_channel(name, activity)
                except UnknownChannelError:
                    retired.add(name)
                else:
                    if channel.archived:
                        retired.add(name)
        return RetiredViews(
            frozenset(thread for thread in threads if not thread.current(snapshot)),
            frozenset(retired),
        )

    def thread_presentations(
        self, names: Iterable[str], *, sent_source_limit: int = 5,
    ) -> ThreadPresentations:
        """Read the requested threads' status once, before loading their messages."""
        snapshot = self.registry.snapshot()
        views = ThreadView.roster(
            snapshot, self.agents, self.goals,
            RosterVisibility(archived=True), names=names,
        )
        return ThreadPresentations(
            self.root, snapshot, {view.thread.name: view for view in views},
            self.transcripts, sent_source_limit,
        )

    def viewer_snapshot(
        self, worktree: Checkout, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> CoordinationSnapshot:
        """Local presentation scope, independent of agent delivery cursors."""
        with self.presentation.snapshot(viewer=self.display_viewer(worktree)) as (basis, source):
            captured_viewer = basis.require_viewer()
            display_activity, display_unread = self.presentation.display_view_metrics(source, basis)
            clocks = source.activity_clocks()
            snapshot = CoordinationSnapshot.capture(
                self.root, basis.registry, basis.catalog, self.agents, basis.channels,
                self.last_sent_timestamps(clocks.sent.items(), registry=basis.registry),
                display_activity,
                goals=self.goals, unread=self.bus.pending_counts_opened(
                    source, basis, conversations=basis.registry.threads,
                ), channel_unread=display_unread,
                visibility=visibility, read_marker_notice=basis.document.notice,
            )
        # Native file IO never holds the display/bus snapshot locks.
        unread = self.transcript_reads.counts(
            captured_viewer, registry=basis.registry, document=basis.document, visibility=visibility,
        )
        return replace(snapshot, thread_unread={**snapshot.thread_unread, **unread.counts})

    def mark_thread_view_read(self, name: str, *, worktree: Checkout, through: TranscriptCursor) -> None:
        """Acknowledge the displayed position through the thread's receiving contract."""
        thread = self.registry.require(name)
        thread.prepare_conversation(ThreadViewRead(
            self, thread, self.messaging.user_identity(worktree), through,
        ))

    def mark_user_view_read(self, target: str, *, worktree: Checkout) -> None:
        """Explicit human 'Mark inbox read' for a channel, DM, or native thread.

        Unlike the agent-facing acknowledgement tool, this never advances an
        executor's delivery cursor. A deliberate mark-read can acknowledge the
        current saved tail without pretending the transcript was displayed.
        """
        CommandTarget.named(target).mark_view_read(self, worktree)

    def mark_thread_user_read(self, target: str, *, worktree: Checkout) -> None:
        viewer = self.messaging.user_identity(worktree).name
        thread = self.registry.require(target)
        checkpoint = self.transcripts.transcript_checkpoint(thread.name)
        thread.prepare_conversation(ThreadViewRead(
            self, thread, self.registry.require(viewer), checkpoint,
        ))
        self.bus.mark_read_through(
            viewer, self.latest_sequence(), target=ThreadTarget(thread.name),
        )

    def latest_sequence(self) -> int:
        """The sequence number of the newest message on the bus, whoever it was sent to."""
        return self.bus.bus.revision()

    def revision(self) -> WireRevision:
        """Stat sources only; strict content reads belong to the requesting view."""
        expiry_tick = int(time.time())
        captured = {
            name: tuple(revision for path in group
                        for revision in FileRevision.capture(path).revisions())
            for name, group in WireRevision.source_paths(self).items()
        }
        return WireRevision(
            **captured, bus_identity=RecordedBusIdentity.capture(self.bus.bus.path),
            bus_seq=self.bus.bus.revision(), expiry_tick=expiry_tick,
        )

    def who(self) -> tuple[ThreadView, ...]:
        """Who is here: live presences, with no unread scan that grows with history."""
        snapshot = self.registry.snapshot()
        live = tuple(thread.name for thread in snapshot.active_threads(snapshot.active_candidates()))
        return tuple(sorted(
            ThreadView.roster(snapshot, self.agents, self.goals, DEFAULT_ROSTER, names=live),
            key=lambda view: view.thread.name))

    def presence(self) -> tuple[ThreadView, ...]:
        """Presence records without viewer-specific unread scans."""
        return tuple(self._presence_views())

    def _presence_views(self):
        return sorted(self.thread_views(), key=lambda view: view.thread.name)

    def list_threads(self, active_only: bool = False) -> tuple[ThreadRecord, ...]:
        """Summarize the default roster's threads with status and pending counts. Archived
        threads have their own readers; listing them here counted thousands of retired inboxes
        on every call."""
        snapshot = self.registry.snapshot()
        threads = tuple(sorted(snapshot.roster(DEFAULT_ROSTER), key=lambda thread: thread.name))
        if active_only:
            threads = tuple(snapshot.active_threads(threads))
        pending = CountedRead().counts(self.bus, tuple(thread.name for thread in threads))
        return ThreadRecord.read(
            snapshot, self.agents, self.goals, threads, pending)

    def thread_detail(self, name: str, read: InboxRead) -> ThreadRecord:
        thread = self.registry.require(name)
        pending = read.counts(self.bus, (thread.name,))
        record, = ThreadRecord.read(self.registry.snapshot(), self.agents,
                                    self.goals, (thread,), pending)
        return record

    def poll(self, target: PollTarget) -> ThreadPoll:
        """One-shot status snapshot: self state plus inbox."""
        name = target.thread(self.registry).name
        peers = tuple(view for view in self._presence_views() if view.thread.name != name)
        return ThreadPoll(self.thread_detail(name, CountedRead()), self.bus.inbox(name).page.messages,
                          self.ledger.read(), peers)
