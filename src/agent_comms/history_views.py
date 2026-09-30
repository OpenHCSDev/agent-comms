"""History, display snapshots and viewer read-state ownership."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .catalog_store import ChannelCatalog
from .goal_pauses import GoalPauseEvents
from .goal_waits import GoalWaits
from .registration import Registration

if TYPE_CHECKING:
    from .historical_views import HistoricalDisplay, HistoricalThread, HistoryCursor, HistorySource
from .agent_activity import AgentActivity
from .bus_activity_index import ChannelActivity
from .channel_management import ChannelManagement
from .channel_targets import BuiltinChannel, is_channel_target
from .collaboration_ledger import CollaborationLedger
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
    ChannelView,
    CoordinationSnapshot,
    MessageNotification,
    ThreadView,
    WireRevision,
)
from .read_basis import ChannelDisplayScope, DMDisplayBasis
from .store_files import _store_lock, file_revision
from .thread_presentation import ThreadPresentation
from .threads import current_thread
from .transcripts import TranscriptCursor, Transcripts

_LOG = logging.getLogger(__name__)


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
        self.presentation = BusPresentation(bus, registry, channels.catalog)
        self.transcript_reads = transcript_read_state(bus.reads.path)

    def message_notifications(self, messages: Sequence[Message]):
        return MessageNotification.window(self.root, self.registry, messages)

    def message_notifications_for_references(self, references: Sequence[MessageReference]):
        """Read mounted references in the notification owner's bounded windows."""
        result = {}
        limit = MessageNotification.window_limit
        for start in range(0, len(references), limit):
            messages = self.bus.log.messages_for_references(references[start : start + limit])
            result.update(self.message_notifications(messages))
        return result

    def recent_notifications(self, name: str, *, limit: int = 5):
        return MessageNotification.recent(self.root, self.registry, self.bus.log, name, limit=limit)

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        return DMHistory(a, b).full_history(self.presentation)

    def channel_history(self, target: str) -> Sequence[Message]:
        targets = self.channels.catalog.read().history_targets(target)
        return ChannelHistory(target, targets).full_history(self.presentation)

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

    def attach_history(self, source_root: Path) -> HistorySource:
        """Attach preserved history without admitting any historical execution."""
        source = self.bus.history.attach(Path(source_root))
        catalog = ChannelCatalog(Path(source.root) / ChannelCatalog.filename)
        incoming = catalog.read()
        source_threads = source.registry().all_threads()
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

    def channel_display_page(
        self,
        target: str,
        *,
        worktree: str | None = None,
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        viewer = self.messaging.user_identity(worktree).name if worktree is not None else None
        if not self.bus.history.sources():
            return self.presentation.channel_page(
                target,
                viewer=viewer,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
        channel = self.channels.catalog.read().views(self.registry.snapshot().threads).get(target)
        if channel is None:
            raise ValueError(f"Unknown channel: {target!r}")
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
        targets = self.channels.catalog.read().history_targets(target)
        return ChannelHistory(target, targets).page(
            self.presentation,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def last_sent_timestamps(self) -> Mapping[str, float]:
        """Canonical projection of the existing target/sender clock authority."""
        snapshot = self.registry.snapshot()
        canonical: dict[str, float] = {}
        for sender, timestamp in self.bus.last_sent_timestamps().items():
            name = snapshot.aliases.get(sender, sender)
            canonical[name] = max(canonical.get(name, 0.0), timestamp)
        return canonical

    def full_history(self) -> Sequence[Message]:
        return ChannelHistory(
            BuiltinChannel.ANY.value, BuiltinChannel.ANY.history_targets
        ).full_history(self.presentation)

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
            with _store_lock(self._wire_lock_path):
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
        return ChannelView.roster(
            snapshot,
            channels,
            catalog.pinned_members(),
            catalog.list_order,
            self.agents.all_activity(),
            self.last_sent_timestamps(),
            ChannelActivity.for_views(channels, self.bus.channel_activity()),
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def thread_views(
        self, *, show_stopped: bool = True, show_archived: bool = False
    ) -> tuple[ThreadView, ...]:
        return ThreadView.roster(
            self.registry.snapshot(),
            self.agents,
            GoalWaits(self.root / GoalWaits.filename),
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def thread_presentation(self, name: str) -> ThreadPresentation | None:
        """Read one current executable thread, including its assigned messages."""
        snapshot = self.registry.snapshot()
        thread = snapshot.threads.get(snapshot.aliases.get(name, name))
        if thread is None:
            return None
        if not ThreadView.visible(thread, snapshot, show_stopped=True, show_archived=False):
            return None
        view = ThreadView.capture(
            thread,
            snapshot,
            self.agents.activity_of(thread.name, snapshot=snapshot),
            self.agents.runtime_info.read().get(thread.name),
            GoalWaits(self.root / GoalWaits.filename).read(),
        )
        return replace(
            view.presentation,
            notifications=self.recent_notifications(thread.name),
            read_identity=self.transcripts.capture_page_read(thread.name).identity,
        )

    def coordination_snapshot(
        self, actor: str = "", *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        channels = self.channel_views(show_stopped=show_stopped, show_archived=show_archived)
        unread = self.bus.pending_counts(actor) if actor in self.registry else {}
        channel_unread: dict[str, int] = {}
        catalog = self.channels.catalog.read()
        for view in channels:
            targets = catalog.history_targets(view.channel.name)
            channel_unread[view.channel.name] = sum(
                count for target, count in unread.items() if targets is None or target in targets
            )
        return CoordinationSnapshot(
            threads=self.thread_views(show_stopped=show_stopped, show_archived=show_archived),
            channels=channels,
            unread=unread,
            last_sent=self.last_sent_timestamps(),
            channel_unread=channel_unread,
            channel_order=catalog.list_order,
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def viewer_snapshot(
        self, worktree: str, *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        """Local presentation scope, independent of agent delivery cursors."""
        viewer = self.messaging.user_identity(worktree).name
        with self.presentation.snapshot(viewer=viewer) as (basis, records, bus_revision):
            registry = basis.registry
            declarations = basis.channels
            scopes = basis.scopes
            order = basis.catalog.list_order
            captured_viewer = basis.viewer
            viewer_names = basis.viewer_names
            pins = basis.catalog.pinned_members()
            notice = basis.notice
            assert captured_viewer is not None
            display_activity, display_unread = self.presentation.display_view_metrics(
                records, scopes, scopes, captured_viewer, viewer_names, bus_revision
            )
            sent = self.last_sent_timestamps()
            channels = ChannelView.roster(
                registry,
                declarations,
                pins,
                order,
                self.agents.all_activity(),
                sent,
                display_activity,
                show_stopped=show_stopped,
                show_archived=show_archived,
            )
            threads = ThreadView.roster(
                registry,
                self.agents,
                GoalWaits(self.root / GoalWaits.filename),
                show_stopped=show_stopped,
                show_archived=show_archived,
            )
            snapshot = CoordinationSnapshot(
                threads=threads,
                channels=channels,
                unread=self.bus.pending_counts(captured_viewer),
                last_sent=sent,
                channel_unread=display_unread,
                channel_order=order,
                show_stopped=show_stopped,
                show_archived=show_archived,
                read_marker_notice=notice,
            )
        # Native file IO never holds the display/bus snapshot locks.
        unread = self.transcript_reads.counts(
            captured_viewer,
            {view.thread.name: view.thread.session_file or "" for view in threads},
        )
        return replace(snapshot, thread_unread=unread.counts, thread_unread_pending=unread.pending)

    def mark_thread_view_read(self, name: str, *, worktree: str, through: TranscriptCursor) -> None:
        """Acknowledge only the native transcript boundary actually displayed."""
        viewer = self.messaging.user_identity(worktree).name
        thread = self.registry.require(name)
        if through.session_file != thread.session_file:
            raise ValueError("Transcript changed; refresh before marking it read.")
        self.transcript_reads.mark_read(viewer, through.session_file, through.offset)

    def mark_channel_view_read(
        self,
        target: str,
        *,
        worktree: str,
        through: int | None = None,
        expected_scope: ChannelDisplayScope | None = None,
    ) -> None:
        self.presentation.mark_channel_read(
            target,
            viewer=self.messaging.user_identity(worktree).name,
            through=through,
            expected_scope=expected_scope,
        )

    def mark_dm_view_read(
        self, peer: str, *, worktree: str, through: int, expected_display_basis: DMDisplayBasis
    ) -> None:
        if type(expected_display_basis) is not DMDisplayBasis:
            raise ValueError("Painted DM read requires a typed page basis and integer bound.")
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
        if is_channel_target(target):
            self.mark_channel_view_read(target, worktree=worktree)
            return
        viewer = self.messaging.user_identity(worktree).name
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(target)
            checkpoint = self.transcripts.transcript_checkpoint(thread.name)
            if checkpoint.session_file and Path(checkpoint.session_file).is_file():
                self.transcript_reads.mark_read(viewer, checkpoint.session_file, checkpoint.offset)
            self.bus.mark_delivered(viewer, thread.name)

    def revision(self) -> WireRevision:
        """A cheap observer token; activity expiry is checked without rescanning idle logs."""
        return WireRevision(
            tuple(
                file_revision(path)
                for path in (
                    self.registry.store.path,
                    self.channels.catalog.path,
                    self.bus.log.path,
                    self.bus.history.path,
                    self.agents.activity._path,
                    self.agents.runtime_info.path,
                    self.bus.reads.path,
                    self.root / GoalWaits.filename,
                )
            ),
            int(time.time()),
        )

    def who(self) -> Sequence[Mapping]:
        """Presence: who is in the chat, with status and unread counts."""
        return self._presence(include_pending=True)

    def presence(self) -> Sequence[Mapping]:
        """Presence without viewer-specific unread scans."""
        return self._presence(include_pending=False)

    def _presence(self, *, include_pending: bool) -> Sequence[Mapping[str, object]]:
        return [
            {
                **view.to_wire(),
                **(
                    {"pending": self.bus.pending_count(view.thread.name)} if include_pending else {}
                ),
            }
            for view in sorted(self.thread_views(), key=lambda view: view.thread.name)
        ]

    def list_threads(self, active_only: bool = False) -> Sequence[Mapping]:
        """Summarize threads with status and pending counts."""
        snapshot = self.registry.snapshot()
        threads = {
            name: thread
            for name, thread in snapshot.threads.items()
            if not active_only or snapshot.statuses[name].active
        }
        activities = self.agents.all_activity(snapshot=snapshot)
        pending = self.bus.pending_counts_all(tuple(threads))
        waits = GoalWaits(self.root / GoalWaits.filename).read()
        return [
            {
                **t.to_wire(),
                "status": snapshot.statuses[name].declared_name,
                "is_fork": t.is_fork,
                "pending": pending[name],
                "goal_pause": (
                    pause.to_wire() if (pause := GoalPauseEvents.for_goal(t.goal)) else None
                ),
                "goal_execution": (
                    asdict(execution)
                    if (execution := GoalWaits.execution(t.goal, waits, snapshot))
                    else None
                ),
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
        detail["goal_execution"] = asdict(execution) if execution else None
        if include_pending:
            detail["pending"] = self.bus.pending_count(name)
        return detail

    def poll(self, name: str | None = None) -> Mapping:
        """One-shot status snapshot: self state plus inbox."""
        if name is None:
            me = current_thread()
            name = me.name
        self.registry.require(name)
        return {
            "thread": self.thread_detail(name),
            "inbox": [m.to_wire() for m in self.bus.inbox(name)],
            "ledger": self.ledger.read(),
            "peers": [person for person in self.presence() if person["name"] != name],
        }
