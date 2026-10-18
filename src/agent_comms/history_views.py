"""History, display snapshots and viewer read-state ownership."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .catalog_store import ChannelCatalog
from .goal_waits import GoalWaits
from .registration import Registration
from .thread_identity import ThreadIncarnation
from .thread_execution import ConversationPreparation
from .transcript_sources import AssignedSourceIdentity

if TYPE_CHECKING:
    from .historical_views import HistoricalThread, HistoryCursor, HistorySource
    from .read_basis import HistoricalDisplay
    from .registry_document import RegistrySnapshot
    from .threads import Thread
from .agent_activity import AgentActivity
from .bus_activity_index import ChannelActivity
from .channel_management import ChannelManagement
from .channel_targets import BuiltinChannel, CommandTarget
from .collaboration_ledger import CollaborationLedger
from .coordination_errors import StaleRevision
from .errors import UnknownChannelError, UnregisteredThreadError
from .exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportReceipt,
    WireExportScope,
    WireTranscriptExporter,
)
from .goal_management import Goals
from .historical_views import ChannelDisplayHistory, ChannelHistory, DMDisplayHistory, DMHistory
from .message_bus import MessageBus
from .message_page import MessagePage
from .message_reference import MessageReference
from .messages import Message
from .messaging import Messaging
from .presentation import (
    ChannelReadMark,
    ReadAll,
    ChannelView,
    CoordinationSnapshot,
    MessageNotification,
    ThreadView,
    WireRevision,
)
from .read_basis import AnonymousViewer, DisplayViewer, DMDisplayBasis, NamedViewer
from .store_files import _store_lock
from .thread_presentation import ThreadPresentation
from .threads import current_thread
from .transcripts import TranscriptCursor, Transcripts

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadViewRead(ConversationPreparation):
    views: HistoryViews
    thread: Thread
    viewer: Thread
    through: TranscriptCursor

    def native(self):
        if self.through.session_file != self.thread.runtime.read_key:
            raise ValueError("Transcript changed; refresh before marking it read.")
        for key in self.thread.runtime.read_keys():
            self.views.transcript_reads.mark_read(self.viewer.name, key, self.through.offset)

    def external(self):
        self.through.wire.require_source(AssignedSourceIdentity(str(self.views.root), self.thread.incarnation))
        page = DMDisplayBasis.fetch_page(
            self.views.bus, self.views.registry, self.views.root, self.viewer.name,
            self.thread.name, worktree=self.viewer.worktree,
            before=self.through.wire.sequence + 1,
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

    def dm_history_page(
        self,
        a: str,
        b: str,
        *,
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Bounded DM history for any client adapter."""

        return DMHistory(a, b).page(
            self.presentation,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def attach_history(self, source_root: Path, *, source_read: HistorySource | None = None) -> HistorySource:
        """Attach preserved history without allowing any historical execution."""
        source = self.bus.history.attach(Path(source_root), source_read=source_read)
        catalog = ChannelCatalog.at(source.root)
        incoming = catalog.read()
        source_threads = source.provenance.threads
        with _store_lock(self._wire_lock_path):
            existing = self.channels.catalog.path.exists()
            with self.channels.catalog.editing() as document:
                document.restore_missing(incoming, source_threads, existing=existing)
        return source

    def historical_threads(self, name: str | None = None) -> tuple[HistoricalThread, ...]:
        return self.bus.history.threads(name)

    def dm_display_page(
        self,
        peer: str,
        *,
        worktree: str,
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        viewer = self.messaging.user_identity(worktree).name
        return DMDisplayHistory(viewer, peer, worktree).page(
            self.presentation,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def display_viewer(self, worktree: str) -> NamedViewer:
        """Select the human reader; page capture resolves it in its own snapshot."""
        return NamedViewer(self.messaging.user_identity(worktree).name)

    def channel_display_page(
        self,
        target: str,
        *,
        viewer: DisplayViewer = AnonymousViewer(),
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        channel = self.channels.catalog.read().require_channel(target, self.registry.snapshot().threads)
        return ChannelDisplayHistory(channel, viewer).page(
            self.presentation,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def mark_historical_view_read(self, displayed: HistoricalDisplay) -> None:
        displayed.acknowledge(self.bus.history, self.registry)

    def channel_history_page(
        self,
        target: str,
        *,
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Bounded channel history for any client adapter."""
        selected = CommandTarget.named(target).require_channel()
        channel = self.channels.catalog.read().resolve(selected.name)
        return ChannelHistory(channel.name, channel.history_targets).page(
            self.presentation,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def last_sent_timestamps(
        self, sent: Mapping[str, float], *, registry: RegistrySnapshot,
    ) -> Mapping[str, float]:
        """Map captured sender clocks through the selected registry names."""
        canonical: dict[str, float] = {}
        for sender, timestamp in sent.items():
            name = registry.canonical_name(sender)
            canonical[name] = max(canonical.get(name, 0.0), timestamp)
        return canonical

    def full_history_page(
        self,
        *,
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        return ChannelHistory(BuiltinChannel.ANY.value, BuiltinChannel.ANY.history_targets).page(
            self.presentation,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
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
    ) -> WireExportReceipt:
        """Export one fixed, non-mutating snapshot of authoritative wire rows."""
        started_at = time.time() if export_started_at is None else export_started_at
        with ExitStack() as stack:
            with _store_lock(self._wire_lock_path, shared=True):
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
        self, *, show_stopped: bool = True, show_archived: bool = False
    ) -> tuple[ChannelView, ...]:
        """Channel declarations and current members; clients never infer membership."""
        snapshot = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        channels = catalog.views(snapshot.threads)
        with self.bus.opened_activity() as clocks:
            return ChannelView.roster(
                snapshot,
                channels.visible(show_archived),
                catalog.pinned_members(),
                catalog.list_order,
                self.agents.all_activity(
                    snapshot.visible_threads(
                        show_stopped=show_stopped, show_archived=show_archived
                    ), snapshot=snapshot,
                ),
                self.last_sent_timestamps(clocks.sent, registry=snapshot),
                ChannelActivity.for_views(channels, clocks.channels),
                show_stopped=show_stopped,
                show_archived=show_archived,
            )

    def thread_views(
        self, *, show_stopped: bool = True, show_archived: bool = False
    ) -> tuple[ThreadView, ...]:
        snapshot = self.registry.snapshot()
        return ThreadView.roster(
            snapshot, self.agents, GoalWaits.at(self.root),
            show_stopped=show_stopped, show_archived=show_archived,
        )

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
                channel = catalog.require_channel(name, snapshot.threads)
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
            names=names, show_stopped=True, show_archived=False,
        )
        return ThreadPresentations(
            snapshot, {view.thread.name: view for view in views}, self.transcripts, sent_source_limit
        )

    def viewer_snapshot(
        self, worktree: str, *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        """Local presentation scope, independent of agent delivery cursors."""
        with self.presentation.snapshot(viewer=self.display_viewer(worktree)) as (basis, source):
            captured_viewer = basis.require_viewer()
            display_activity, display_unread = self.presentation.display_view_metrics(source, basis)
            clocks = self.bus.activity_index.snapshot(source)
            snapshot = CoordinationSnapshot.capture(
                self.root, basis.registry, basis.catalog, self.agents, basis.channels,
                self.last_sent_timestamps(clocks.sent, registry=basis.registry), display_activity,
                unread=self.bus.pending_counts_opened(source, basis), channel_unread=display_unread,
                show_stopped=show_stopped, show_archived=show_archived,
                read_marker_notice=basis.document.notice,
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
        return [{**view.to_wire(), "pending": pending[view.thread.name]} for view in views]

    def presence(self) -> Sequence[Mapping]:
        """Presence without viewer-specific unread scans."""
        return [view.to_wire() for view in self._presence_views()]

    def _presence_views(self):
        return sorted(self.thread_views(), key=lambda view: view.thread.name)

    def list_threads(self, active_only: bool = False) -> Sequence[Mapping]:
        """Summarize threads with status and pending counts."""
        snapshot = self.registry.snapshot()
        threads = {
            name: thread
            for name, thread in snapshot.threads.items()
            if not active_only or snapshot.statuses[name].active
        }
        activities = self.agents.all_activity(
            (thread for name, thread in threads.items() if snapshot.statuses[name].active),
            snapshot=snapshot,
        )
        pending = self.bus.pending_counts_all(tuple(threads))
        waits = GoalWaits.at(self.root).read()
        return [
            {
                **t.to_wire(),
                "status": snapshot.statuses[name].declared_name,
                "is_fork": t.is_fork,
                "pending": pending[name],
                "goal_execution": GoalWaits.execution(t.goal, waits, snapshot, self.agents).to_wire(),
                "activity": activities[name].state.value if name in activities else "idle",
                "activity_detail": activities[name].detail if name in activities else "",
            }
            for name, t in sorted(threads.items())
        ]

    def thread_detail(self, name: str, *, include_pending: bool = True) -> Mapping[str, object]:
        t = self.registry.require(name)
        detail = {
            **t.to_wire(),
            "status": self.registry.status(name).declared_name,
            "is_fork": t.is_fork,
        }
        execution = self.goals.goal_execution(name)
        detail["goal_execution"] = execution.to_wire()
        if include_pending:
            detail["pending"] = self.bus.pending_count(name)
        return detail

    def poll(self, name: str | None = None) -> Mapping:
        """One-shot status snapshot: self state plus inbox."""
        if name is None:
            me = current_thread(self.registry)
            name = me.name
        self.registry.require(name)
        return {
            "thread": self.thread_detail(name),
            "inbox": [m.to_wire() for m in self.bus.inbox(name)],
            "ledger": self.ledger.read(),
            "peers": [person for person in self.presence() if person["name"] != name],
        }
