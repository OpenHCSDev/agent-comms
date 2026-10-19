"""History, display snapshots and viewer read-state ownership."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from .catalog_store import ChannelCatalog
from .cli_result import CliResult
from .field_codec import FieldCodec, WireForm
from .goal_waits import GoalWaits
from .registration import Registration
from .registry_document import DEFAULT_ROSTER, RosterVisibility
from .shared_ledger import LedgerDocument
from .thread_identity import ThreadIncarnation
from .thread_execution import ConversationPreparation
from .transcript_sources import AssignedSourceIdentity

if TYPE_CHECKING:
    from .historical_views import HistoricalThread, HistorySource
    from .read_basis import HistoricalDisplay
    from .registry_document import RegistrySnapshot
    from .threads import Thread
from .agent_activity import AgentActivity
from .bus_activity_index import ChannelActivity
from .channel_management import ChannelManagement
from .channel_targets import BuiltinChannel, CommandTarget
from .read_basis import AllChannelDisplayScope, ChannelDisplayScope
from .channels import ChannelMemberIndex
from .collaboration_ledger import CollaborationLedger
from .coordination_errors import StaleRevision
from .errors import UnknownChannelError, UnregisteredThreadError
from .exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportProvenance,
    WireExportScope,
    WireTranscriptExporter,
)
from .goal_management import Goals
from .historical_views import ChannelDisplayHistory, ChannelHistory, DMDisplayHistory, DMHistory
from .message_bus import MessageBus
from .message_page import NEWEST_PAGE, MessagePage, PageBounds
from .page_traversal import OlderTraversal
from .message_reference import MessageReference
from .messages import Message
from .messaging import Messaging
from .presentation import (
    ChannelReadMark,
    ReadAll,
    ChannelView,
    CoordinationSnapshot,
    MessageNotification,
    ThreadRecord,
    ThreadView,
    WireRevision,
)
from .read_basis import AnonymousViewer, DisplayViewer, DMDisplayBasis, NamedViewer
from .store_files import SharedLock, _store_lock
from .thread_presentation import ThreadPresentation
from .threads import current_thread
from .transcripts import TranscriptCursor, Transcripts

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadPoll(CliResult):
    """One thread's state, its inbox, the shared ledger and its peers at one read."""
    thread: ThreadRecord
    inbox: tuple[Annotated[Message, WireForm], ...]
    ledger: Annotated[LedgerDocument, WireForm]
    peers: tuple[ThreadView, ...]

    def encoded(self) -> object:
        return FieldCodec.project(self, "presence")


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
            self.views.bus, self.views.registry, self.views.root, self.viewer.name,
            self.thread.name, worktree=self.viewer.worktree,
            bounds=PageBounds(OlderTraversal(self.through.wire.sequence + 1)),
        )
        if page.messages:
            self.views.mark_dm_view_read(
                self.thread.name, worktree=self.viewer.worktree,
                through=page.messages[-1].seq, expected_display_basis=page.display_basis,
            )


@dataclass(frozen=True)
class RetiredViews:
    """Views whose source no longer exists, as Core decided from one registry read."""

    threads: frozenset[ThreadIncarnation]
    channels: frozenset[str]


@dataclass(frozen=True)
class ThreadPresentations:
    """One status acquisition shared by a reader's thread presentations.

    These rows belong to this registry cut, not a cache. Source acquisition
    remains per thread, so one busy or damaged source cannot hide its peers.
    """

    snapshot: RegistrySnapshot
    views: Mapping[str, ThreadView]
    transcripts: Transcripts
    sent_source_limit: int

    def view(self, name: str) -> ThreadView | None:
        return self.views.get(self.snapshot.canonical_name(name))

    def read(self, name: str) -> ThreadPresentation | None:
        view = self.view(name)
        if view is None:
            return None
        captured = self.transcripts.capture_page_window(
            view.thread.name, source_limit=5, sent_source_limit=self.sent_source_limit
        )
        owner = captured.read.identity.thread
        if not owner.incarnation.current(self.snapshot):
            raise StaleRevision("Thread source incarnation changed during presentation")
        return view.presentation.with_sources(captured)

    def require(self, name: str) -> ThreadPresentation:
        """A bound caller must name an available thread, not an anonymous view."""
        presentation = self.read(name)
        if presentation is None:
            raise UnregisteredThreadError(f"Thread presentation unavailable: {name}")
        return presentation


class HistoryViews:
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
        from .presentation import BusPresentation
        from .view_unread import transcript_read_state

        self.root = root
        self.registry = registry
        self.bus = bus
        self.channels = channels
        self.agents = agents
        self.transcripts = transcripts
        self.messaging = messaging
        self.goals = goals
        self.ledger = ledger
        self._wire_lock_path = root / "wire"
        self.presentation = BusPresentation(bus, registry, channels.catalog, agents)
        self.transcript_reads = transcript_read_state(bus.reads.path)

    def message_notifications_for_references(self, references: Sequence[MessageReference]):
        return MessageNotification.references(self.root, self.registry, self.bus.log, references)

    def dm_history_page(self, a: str, b: str, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        """Bounded DM history for any client adapter."""
        return DMHistory(a, b).page(self.presentation, bounds)

    def attach_history(self, source_root: Path, *, source_read: HistorySource | None = None) -> HistorySource:
        """Attach preserved history without allowing any historical execution."""
        source = self.bus.history.attach(Path(source_root), source_read=source_read)
        catalog = ChannelCatalog.at(source.root)
        incoming = catalog.read()
        with _store_lock(self._wire_lock_path):
            existing = self.channels.catalog.path.exists()
            with self.channels.catalog.editing() as document:
                document.restore_missing(incoming, existing=existing)
        return source

    def historical_threads(self, name: str | None = None) -> tuple[HistoricalThread, ...]:
        return self.bus.history.threads(name)

    def dm_display_page(
        self, peer: str, *, worktree: str, bounds: PageBounds = NEWEST_PAGE
    ) -> MessagePage:
        viewer = self.messaging.user_identity(worktree).name
        return DMDisplayHistory(viewer, peer, worktree).page(self.presentation, bounds)

    def display_viewer(self, worktree: str) -> NamedViewer:
        """Select the human reader; page capture resolves it in its own snapshot."""
        return NamedViewer(self.messaging.user_identity(worktree).name)

    def channel_display_page(
        self,
        target: str,
        *,
        viewer: DisplayViewer = AnonymousViewer(),
        bounds: PageBounds = NEWEST_PAGE,
    ) -> MessagePage:
        channel = self.channels.catalog.read().require_channel(target)
        return ChannelDisplayHistory(channel, viewer).page(self.presentation, bounds)

    def mark_historical_view_read(self, displayed: HistoricalDisplay) -> None:
        displayed.acknowledge(self.bus.history, self.registry)

    def channel_history_page(self, target: str, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        """Bounded channel history for any client adapter."""
        selected = CommandTarget.named(target).require_channel()
        channel = self.channels.catalog.read().require_channel(selected.name)
        return ChannelHistory(channel.history_scope()).page(self.presentation, bounds)

    def last_sent_timestamps(
        self, rows: Iterable[tuple[str, float]], *, registry: RegistrySnapshot,
    ) -> dict[str, float]:
        """Map each supplied sender clock through its current registry name."""
        canonical: dict[str, float] = {}
        for sender, timestamp in rows:
            name = registry.canonical_name(sender)
            canonical[name] = max(canonical.get(name, 0.0), timestamp)
        return canonical

    def full_history_page(self, bounds: PageBounds = NEWEST_PAGE) -> MessagePage:
        return ChannelHistory(AllChannelDisplayScope(BuiltinChannel.ANY.value)).page(
            self.presentation, bounds
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
    ) -> WireExportProvenance:
        """Export one fixed, non-mutating snapshot of authoritative wire rows."""
        started_at = time.time() if export_started_at is None else export_started_at
        with ExitStack() as stack:
            with _store_lock(self._wire_lock_path, mode=SharedLock()):
                resolved = scope.resolve(self.channels.catalog, self.registry)
                through, messages = stack.enter_context(self.bus.log.full_history_snapshot())

            selected = (message for message in messages if resolved.matches(message))
            return WireTranscriptExporter(
                format=format,
                scope=resolved.scope,
                limit=limit,
                boundary=WireExportBoundary(through, started_at),
            ).export(selected, Path(destination).expanduser(), overwrite=overwrite)

    def channel_views(
        self, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> tuple[ChannelView, ...]:
        """Channel declarations and current members; clients never infer membership."""
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        channels = catalog.views()
        with self.bus.opened_activity() as clocks:
            return ChannelView.roster(
                snapshot,
                channels.visible(visibility),
                catalog.pinned_members(),
                catalog.list_order,
                self.agents.all_activity(
                    snapshot.visible_threads(visibility), snapshot=snapshot,
                ),
                self.last_sent_timestamps(clocks.sent.items(), registry=snapshot),
                ChannelActivity.for_views(channels, clocks.channels),
                visibility,
            )

    def channel_view(self, name: str, visibility: RosterVisibility = DEFAULT_ROSTER) -> ChannelView:
        """Read one channel; observe and order only its own members."""
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        channel = catalog.require_channel(name)
        registered = channel.members(ChannelMemberIndex(snapshot.threads.values()))
        with self.bus.opened_activity() as clocks:
            rows = ((alias, clocks.sent[alias]) for thread in registered
                    for alias in snapshot.names_for(thread.name) if alias in clocks.sent)
            sent = self.last_sent_timestamps(rows, registry=snapshot)
            history = ChannelActivity.for_views(
                {channel.name: channel}, clocks.channels)[channel.name]
            return ChannelView.capture(
                channel, registered, snapshot, catalog.pinned_threads(channel.name),
                self.agents.all_activity(registered, snapshot=snapshot), sent, history, visibility,
            )

    def thread_views(
        self, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> tuple[ThreadView, ...]:
        snapshot = self.registry.snapshot()
        return ThreadView.roster(snapshot, self.agents, GoalWaits.at(self.root), visibility)

    def retired_views(self, threads: Iterable[ThreadIncarnation], channels: Iterable[str]) -> RetiredViews:
        """Which of these thread incarnations and channel views no longer exist.

        A thread is retired when it was deleted or replaced by a newer
        incarnation; a channel when it is gone from the catalog or archived.
        """
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        retired = set()
        for name in channels:
            try:
                channel = catalog.require_channel(name)
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
        """Read status once for only the requested rows, before their original sources."""
        snapshot = self.registry.snapshot()
        views = ThreadView.roster(
            snapshot, self.agents, GoalWaits.at(self.root),
            DEFAULT_ROSTER, names=names,
        )
        return ThreadPresentations(
            snapshot, {view.thread.name: view for view in views}, self.transcripts, sent_source_limit
        )

    def viewer_snapshot(
        self, worktree: str, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> CoordinationSnapshot:
        """Local presentation scope, independent of agent delivery cursors."""
        with self.presentation.snapshot(viewer=self.display_viewer(worktree)) as (basis, source):
            captured_viewer = basis.require_viewer()
            display_activity, display_unread = self.presentation.display_view_metrics(source, basis)
            clocks = self.bus.activity_index.snapshot(source)
            snapshot = CoordinationSnapshot.capture(
                self.root, basis.registry, basis.catalog, self.agents, basis.channels,
                self.last_sent_timestamps(clocks.sent.items(), registry=basis.registry),
                display_activity,
                unread=self.bus.pending_counts_opened(source, basis), channel_unread=display_unread,
                visibility=visibility, read_marker_notice=basis.document.notice,
            )
        # Native file IO never holds the display/bus snapshot locks.
        sources = {
            name: view.thread.runtime.read_keys()
            for name, view in snapshot.threads.items()
        }
        unread = self.transcript_reads.counts(captured_viewer, sources, document=basis.document)
        return replace(snapshot, thread_unread=unread.counts, unread_pending=unread.pending)

    def mark_thread_view_read(self, name: str, *, worktree: str, through: TranscriptCursor) -> None:
        """Acknowledge the displayed position through the thread's receiving contract."""
        thread = self.registry.require(name)
        thread.execution.prepare_conversation(ThreadViewRead(
            self, thread, self.messaging.user_identity(worktree), through,
        ))

    def mark_channel_view_read(
        self,
        target: str,
        *,
        worktree: str,
        mark: ChannelReadMark = ReadAll(),
    ) -> None:
        self.presentation.mark_channel_read(
            target,
            viewer=self.messaging.user_identity(worktree).name,
            mark=mark,
        )

    def mark_dm_view_read(
        self, peer: str, *, worktree: str, through: int, expected_display_basis: DMDisplayBasis
    ) -> None:
        expected_display_basis.acknowledge(
            self.bus,
            self.registry,
            self.root,
            peer,
            worktree=worktree,
            through=through,
        )

    def mark_user_view_read(self, target: str, *, worktree: str) -> None:
        """Explicit human 'Mark inbox read' for a channel, DM, or native thread.

        Unlike the agent-facing acknowledgement tool, this never advances an
        executor's delivery cursor. A deliberate mark-read can acknowledge the
        current saved tail without pretending the transcript was displayed.
        """
        CommandTarget.named(target).mark_view_read(self, worktree)

    def mark_thread_user_read(self, target: str, *, worktree: str) -> None:
        viewer = self.messaging.user_identity(worktree).name
        thread = self.registry.require(target)
        checkpoint = self.transcripts.transcript_checkpoint(thread.name)
        thread.execution.prepare_conversation(ThreadViewRead(
            self, thread, self.registry.require(viewer), checkpoint,
        ))
        messages = self.bus.inbox(viewer, thread.name)
        self.bus.mark_delivered(viewer, tuple(message.reference for message in messages))

    def latest_sequence(self) -> int:
        """The sequence number of the newest message on the bus, whoever it was sent to."""
        return self.bus.log.latest_sequence()

    def revision(self) -> WireRevision:
        """A cheap observer token; activity expiry is checked without rescanning idle logs."""
        return WireRevision.capture(self, expiry_tick=int(time.time()))

    def who(self) -> Sequence[Mapping]:
        """Presence: who is in the chat, with status and unread counts."""
        views = self._presence_views()
        # One bus snapshot for every thread, not one per thread.
        pending = self.bus.pending_counts_all(tuple(view.thread.name for view in views))
        counted = (replace(view, pending=pending[view.thread.name]) for view in views)
        return [FieldCodec.project(view, "presence") for view in counted]

    def presence(self) -> Sequence[Mapping]:
        """Presence without viewer-specific unread scans."""
        return [FieldCodec.project(view, "presence") for view in self._presence_views()]

    def _presence_views(self):
        return sorted(self.thread_views(), key=lambda view: view.thread.name)

    def list_threads(self, active_only: bool = False) -> Sequence[Mapping]:
        """Summarize threads with status and pending counts."""
        snapshot = self.registry.snapshot()
        threads = tuple(thread for name, thread in sorted(snapshot.threads.items())
                        if not active_only or snapshot.statuses[name].active)
        pending = self.bus.pending_counts_all(tuple(thread.name for thread in threads))
        return [FieldCodec.project(record, "presence") for record in ThreadRecord.read(
            snapshot, self.agents, GoalWaits.at(self.root).read(), threads, pending)]

    def thread_detail(self, name: str, *, include_pending: bool = True) -> ThreadRecord:
        thread = self.registry.require(name)
        pending = self.bus.pending_counts_all((thread.name,)) if include_pending else None
        record, = ThreadRecord.read(self.registry.snapshot(), self.agents,
                                    GoalWaits.at(self.root).read(), (thread,), pending)
        return record

    def poll(self, name: str | None = None) -> ThreadPoll:
        """One-shot status snapshot: self state plus inbox."""
        if name is None:
            me = current_thread(self.registry)
            name = me.name
        self.registry.require(name)
        peers = tuple(view for view in self._presence_views() if view.thread.name != name)
        return ThreadPoll(self.thread_detail(name), tuple(self.bus.inbox(name)),
                          self.ledger.document(), peers)
