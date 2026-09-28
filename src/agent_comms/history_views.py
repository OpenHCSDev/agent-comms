"""History, display snapshots and viewer read-state ownership."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .catalog_store import ChannelCatalog
from .goal_pauses import GoalPauseEvents
from .goal_waits import GoalWaits
from .registration import Registration

if TYPE_CHECKING:
    from .historical_views import HistoricalDisplay, HistoricalThread, HistoryCursor, HistorySource
from .activity import Activity, ActivityState
from .agent_activity import AgentActivity
from .bus_activity_index import ChannelActivity
from .channel_management import ChannelManagement
from .channel_targets import BuiltinChannel, is_channel_target
from .channels import Channel
from .collaboration_ledger import CollaborationLedger
from .display_order import ChannelSort
from .exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportReceipt,
    WireExportScope,
    WireTranscriptExporter,
)
from .goal_management import Goals
from .historical_views import ChannelDisplayHistory, ChannelHistory, DMHistory, HistoryView
from .message_bus import MessageBus
from .message_page import MessagePage
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
from .registry_document import RegistrySnapshot
from .store_files import _store_lock, file_revision
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
        self.presentation = BusPresentation(bus.log.path)
        self.transcript_reads = transcript_read_state(bus.reads.path)
        self._sent_times_signature: tuple[int, int, int] | None = None
        self._sent_times: dict[str, float] = {}

    def message_notifications(
        self, messages: Sequence[Message]
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Read one visible window's actual recipient outcomes; never schedule work.

        Display uses the coordinator's existing assignment decoder and lifecycle.
        A missing legacy store or absent row supplies no receipt. Errors remain
        visible to the caller instead of becoming false successful delivery.
        """
        if len(messages) > 120:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = {(message.seq, message.message_id) for message in messages if message.seq > 0}
        result: dict[tuple[int, str], list[MessageNotification]] = {key: [] for key in keys}
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = self._notification_rows(
            f"w.wire_seq IN ({placeholders})", tuple(key[0] for key in keys)
        )
        for row, notification in self._project_notifications(rows):
            key = (row["wire_seq"], row["message_id"])
            if key in result:
                result[key].append(notification)
        return {key: tuple(rows) for key, rows in result.items()}

    def recent_notifications(self, name: str, *, limit: int = 5) -> tuple[MessageNotification, ...]:
        """Recent assigned messages, including channel work, for one agent view.

        Reads the original assignments and source messages. This is not a DM
        delivery or a read acknowledgement, and never schedules another turn.
        """
        from .bus_publication import stable_thread_lookup

        if not 1 <= limit <= 20:
            raise ValueError("Recent notification limit must be between 1 and 20")
        owner = self.registry.require(name)
        rows = self._notification_rows(
            "w.recipient_lookup=?",
            (stable_thread_lookup(owner.created_at),),
            limit=limit,
        )
        result = []
        for row, notification in self._project_notifications(rows):
            message = self.bus.log.message_by_id(row["message_id"])
            if message is not None and message.seq == row["wire_seq"]:
                result.append(replace(notification, message=message))
        return tuple(result)

    def _notification_rows(self, predicate: str, parameters: tuple, *, limit: int = 0):
        import sqlite3
        from contextlib import closing

        from .coordination import COORDINATION_SCHEMA_VERSION
        from .recovery_projection import _preflight

        database = self.root / "coordination.sqlite3"
        failure = _preflight(database)
        if failure == "missing":
            return ()
        if failure:
            raise ValueError(f"Channel notification status unavailable: {failure}")
        with closing(sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True,
            timeout=0.05, isolation_level=None,
        )) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if (
                connection.execute("PRAGMA user_version").fetchone()[0]
                != COORDINATION_SCHEMA_VERSION
            ):
                raise ValueError("Channel notification status has an unsupported schema")
            return tuple(connection.execute(
                "SELECT w.*, EXISTS (SELECT 1 FROM native_runtime_inputs n "
                "WHERE n.claim_id=w.claim_id AND n.stage='triage' AND n.verdict IS NULL) "
                "AS triage_inflight, c.execution_id AS current_execution_id FROM wake_claims w "
                "LEFT JOIN current_executions c ON c.owner_lookup=w.recipient_lookup "
                f"WHERE {predicate} ORDER BY w.wire_seq DESC,w.recipient"
                + (" LIMIT ?" if limit else ""),
                (*parameters, limit) if limit else parameters,
            ))

    def _project_notifications(self, rows):
        from .bus_publication import stable_thread_lookup
        from .coordination_store import _assignment
        from .owner_lifecycle import OwnerLifecycle

        snapshot = self.registry.snapshot()
        owners = {
            stable_thread_lookup(thread.created_at): thread
            for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].active and thread.pid > 0
        }
        for row in rows:
            assignment = _assignment(row)
            owner = owners.get(assignment.recipient_lookup)
            current_turn = bool(
                owner is not None
                and owner.active_turn is not None
                and owner.active_turn.owner_pid == owner.pid
                and owner.active_turn.started_at * 1000 <= assignment.updated_at_ms + 1
                and OwnerLifecycle._process_alive(owner.pid)
            )
            yield row, assignment.lifecycle.notification(
                assignment.recipient,
                owner_active=owner is not None,
                current_turn=current_turn,
                triage_inflight=bool(row["triage_inflight"]),
                blocked_by_prior=bool(
                    row["current_execution_id"] is not None
                    and row["current_execution_id"] != assignment.lifecycle.execution_id
                ),
                prior_turn_active=bool(owner is not None and owner.active_turn is not None),
            )

    @staticmethod
    def _complete_history(page_reader):
        """Materialize only for callers explicitly requesting the full history."""
        page = page_reader(limit=1000)
        pages = [page.messages]
        while page.has_older:
            page = page_reader(before=page.oldest_cursor, limit=1000)
            pages.append(page.messages)
        return [message for rows in reversed(pages) for message in rows]

    def dm_history(self, a: str, b: str) -> Sequence[Message]:
        """Full conversation between two threads, in seq order."""
        return self._complete_history(lambda **kwargs: self.dm_history_page(a, b, **kwargs))

    def channel_history(self, target: str) -> Sequence[Message]:
        """Full history of one channel (``#all`` or a tag channel)."""
        return self._complete_history(lambda **kwargs: self.channel_history_page(target, **kwargs))

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

        return self._integrated_display_page(
            lambda **kwargs: self.bus.dm_history_page(a, b, **kwargs),
            DMHistory(a, b),
            worktree=None,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def attach_history(self, source_root: Path) -> HistorySource:
        """Attach preserved history without admitting any historical execution."""
        source = self.bus.attach_history(Path(source_root))
        catalog = ChannelCatalog(Path(source.root) / ChannelCatalog.filename)
        incoming = catalog.read()
        source_threads = source.registry().all_threads()
        with _store_lock(self._wire_lock_path):
            existing = any(path.exists() for path in self.channels.catalog.source_paths())
            with self.channels.catalog.editing() as document:
                document.restore_missing(incoming, source_threads, existing=existing)
        return source

    def historical_threads(self, name: str | None = None) -> tuple[HistoricalThread, ...]:
        from .historical_views import HistoricalThread

        return tuple(
            HistoricalThread(source, thread)
            for source in self.bus.history_sources()
            for thread in source.registry().snapshot().threads.values()
            if name is None or thread.name == name
        )

    def _integrated_display_page(
        self, live_page, view: HistoryView, *, worktree, before, after, limit, max_bytes
    ):
        from .historical_views import HistoricalDisplay, HistoryCursor

        if not self.bus.history_sources() and not isinstance(before or after, HistoryCursor):
            return live_page(before=before, after=after, limit=limit, max_bytes=max_bytes)
        history_revision = file_revision(self.bus.history_manifest)
        if before is not None and after is not None:
            raise ValueError("Choose one history paging direction")
        cursor = before if before is not None else after
        historical = isinstance(cursor, HistoryCursor)
        if not historical:
            page = live_page(before=before, after=after, limit=limit, max_bytes=max_bytes)
            if page.messages or after is not None:
                return replace(
                    page,
                    has_older=page.has_older or bool(self.bus.history_sources()),
                    history_revision=history_revision,
                )
        history = self.bus.historical_page(
            view,
            before=before if historical and before is not None else None,
            after=after if historical and after is not None else None,
            limit=limit,
            max_bytes=max_bytes,
        )
        if history.messages:
            display = None
            if worktree is not None:
                viewer = self.messaging.user_identity(worktree)
                source = history.messages[0].source
                display = HistoricalDisplay(
                    source,
                    self.bus.reads.capture(
                        viewer.name,
                        history.messages,
                        self.registry.snapshot(),
                        Path(source.root) / "bus.jsonl",
                        conversation_snapshot=source.registry().snapshot(),
                    ),
                )
            latest = live_page(limit=1)
            return replace(
                history,
                historical_display=display,
                history_revision=history_revision,
                has_newer=history.has_newer or bool(latest.messages),
            )
        if historical and after is not None:
            return replace(
                live_page(after=0, limit=limit, max_bytes=max_bytes),
                history_revision=history_revision,
            )
        return replace(history if historical else page, history_revision=history_revision)

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

        historical_only = peer not in self.registry and bool(self.historical_threads(peer))

        def live_page(**kwargs):
            if historical_only:
                return MessagePage((), False, False)
            return self._live_dm_display_page(peer, worktree=worktree, **kwargs)

        return self._integrated_display_page(
            live_page,
            DMHistory(viewer, peer),
            worktree=worktree,
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
        if not self.bus.history_sources():
            return self._live_channel_display_page(
                target,
                worktree=worktree,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
        channel = self.channels.catalog.read().views(self.registry.snapshot().threads).get(target)
        if channel is None:
            raise ValueError(f"Unknown channel: {target!r}")

        return self._integrated_display_page(
            lambda **kwargs: self._live_channel_display_page(target, worktree=worktree, **kwargs),
            ChannelDisplayHistory(channel),
            worktree=worktree,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def mark_historical_view_read(self, displayed: HistoricalDisplay) -> None:
        """Record only painted historical membership in the existing human ledger."""
        viewer = self.registry.require(displayed.viewer)
        if viewer.created_at != displayed.viewer_created_at or viewer.role.executable:
            raise ValueError("Historical viewer changed; refresh history")
        if displayed.source not in self.bus.history_sources():
            raise ValueError("Historical source detached; refresh history")
        displayed.source.validate()
        # The snapshot is a separate bus: reuse its existing ledger owner and
        # schema. Live/older readers never encounter foreign sequence facts.
        from .read_ledger import ReadLedger

        ReadLedger(Path(displayed.source.root) / ReadLedger.filename).mark_displayed(
            displayed.viewer, displayed.displayed
        )

    def _live_dm_display_page(
        self,
        peer: str,
        *,
        worktree: str,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Fetch a bounded human DM page and its immutable identity/read basis.

        Fetching is not paint proof. A UI may use the basis only after proving
        that the corresponding inbound tail was contiguous and visibly painted.
        """
        if not isinstance(peer, str) or is_channel_target(peer) or BuiltinChannel.is_alias(peer):
            raise ValueError("A DM page requires a registered peer.")
        viewer = self.messaging.user_identity(worktree).name
        marker_path = self.bus.reads.path
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            revision = file_revision(self.registry.store.path)
            viewer_name = snapshot.aliases.get(viewer, viewer)
            peer_name = snapshot.aliases.get(peer, peer)
            viewer_thread = snapshot.threads.get(viewer_name)
            peer_thread = snapshot.threads.get(peer_name)
            if (
                viewer_thread is None
                or not self.bus.reads.human(viewer_thread.role)
                or peer_thread is None
                or viewer_name == peer_name
            ):
                raise ValueError("DM display identity changed; refresh the page.")
            viewer_names = frozenset(
                {
                    viewer_name,
                    *(name for name, owner in snapshot.aliases.items() if owner == viewer_name),
                }
            )
            peer_names = frozenset(
                {
                    peer_name,
                    *(name for name, owner in snapshot.aliases.items() if owner == peer_name),
                }
            )
            marker_revision = file_revision(marker_path)
            page = self.bus.dm_history_page(
                viewer_name,
                peer_name,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
            older_unread = False
            if page.has_older and page.oldest_seq is not None:
                seen = self.bus.reads.seen_sequences(viewer_name, snapshot)
                with self.bus.log._record_snapshot(need_sequence=False) as (_, records):
                    older_unread = any(
                        message.seq < page.oldest_seq
                        and message.seq not in seen
                        and message.sender in peer_names
                        and message.target in viewer_names
                        for message, _ in records
                    )
            if (
                file_revision(self.registry.store.path) != revision
                or file_revision(marker_path) != marker_revision
            ):
                raise ValueError("DM display changed while paging; refresh the page.")
            root_info = self.root.stat()
            try:
                bus_info = self.bus.log.path.stat()
            except FileNotFoundError:
                bus_identity = None
            else:
                bus_identity = (bus_info.st_dev, bus_info.st_ino)
            return replace(
                page,
                display_basis=DMDisplayBasis(
                    root=str(self.root.resolve()),
                    root_identity=(root_info.st_dev, root_info.st_ino),
                    worktree=str(Path(worktree).resolve()),
                    requested_peer=peer,
                    viewer_identity=viewer_thread.incarnation,
                    viewer_names=viewer_names,
                    peer_identity=peer_thread.incarnation,
                    peer_names=peer_names,
                    marker_revision=marker_revision,
                    bus_identity=bus_identity,
                    newest_seq=page.newest_seq,
                    older_unread=older_unread,
                    displayed=self.bus.reads.capture(
                        viewer_name, page.messages, snapshot, self.bus.log.path
                    ),
                ),
            )

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
        return self._integrated_display_page(
            lambda **kwargs: self.bus.channel_history_page(target, **kwargs),
            ChannelHistory(target, targets),
            worktree=None,
            before=before,
            after=after,
            limit=limit,
            max_bytes=max_bytes,
        )

    def _display_basis_revision(self) -> tuple:
        """Revisions of every store used to define one display predicate and marker."""
        return tuple(
            file_revision(path)
            for path in (
                self.registry.store.path,
                *self.channels.catalog.source_paths(),
                self.bus.reads.path,
            )
        )

    def _capture_display_basis(self, viewer: str | None) -> tuple:
        """Capture one basis while the caller holds the short wire lock."""
        registry = self.registry.snapshot()
        catalog = self.channels.catalog.read()
        channels = catalog.views(registry.threads)
        order = catalog.list_order
        pins = catalog.pinned_members()
        seen = (
            self.bus.reads.seen_sequences(viewer, registry) if viewer is not None else frozenset()
        )
        canonical_viewer = registry.aliases.get(viewer, viewer) if viewer is not None else None
        viewer_names = (
            frozenset(
                {
                    canonical_viewer,
                    *(
                        name
                        for name, owner in registry.aliases.items()
                        if owner == canonical_viewer
                    ),
                }
            )
            if canonical_viewer is not None
            else frozenset()
        )
        scopes = tuple(
            ChannelDisplayScope.capture(channel, registry, seen=seen)
            for channel in channels.values()
        )
        notice = self.bus.reads.read().notice
        return (
            registry,
            channels,
            scopes,
            order,
            canonical_viewer,
            viewer_names,
            pins,
            notice,
        )

    @contextmanager
    def _display_snapshot(
        self, viewer: str | None = None, target: str | None = None
    ) -> Iterator[tuple]:
        """Open one bus boundary atomically with a validated display basis.

        Direct registry/catalog writers need not hold the Comms wire lock; the
        revision checks detect their changes before the opened bus boundary.
        The raw scan happens after releasing that lock. A later append is not
        retried: the opened inode and byte limit already define a valid point.
        """
        for _ in range(3):
            with ExitStack() as stack:
                with _store_lock(self._wire_lock_path):
                    revision = self._display_basis_revision()
                    basis = self._capture_display_basis(viewer)
                    if self._display_basis_revision() != revision:
                        continue
                    bus_revision = file_revision(self.bus.log.path)
                    _, records = stack.enter_context(
                        self.bus.log._record_snapshot(need_sequence=False)
                    )
                    if self._display_basis_revision() != revision:
                        continue
                    if file_revision(self.bus.log.path) != bus_revision:
                        bus_revision = None
                if target is not None and target not in basis[1]:
                    # A newly-created channel must not be rejected from an
                    # earlier basis without checking its current revision.
                    if self._display_basis_revision() != revision:
                        continue
                    raise ValueError(f"Unknown channel: {target!r}")
                yield basis, records, bus_revision
                return
        raise RuntimeError("Display scope changed during snapshot; retry the request.")

    def _live_channel_display_page(
        self,
        target: str,
        *,
        worktree: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Local display projection; underlying channel history remains target-owned."""
        if not is_channel_target(target):
            raise ValueError(f"{target!r} is not a channel target.")
        viewer = self.messaging.user_identity(worktree).name if worktree is not None else None
        with self._display_snapshot(viewer=viewer, target=target) as (basis, records, _):
            scope = next(item for item in basis[2] if item.channel == target)
            page = self.bus._collect_history_page(
                records,
                scope.includes,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
            if viewer is not None:
                scope = replace(
                    scope,
                    displayed=self.bus.reads.capture(
                        viewer, page.messages, basis[0], self.bus.log.path
                    ),
                )
            return replace(page, display_scope=scope)

    def last_sent_timestamps(self) -> Mapping[str, float]:
        """Latest explicit outgoing message per sender, cached until the log changes."""
        path = self.root / "bus.jsonl"
        try:
            stat = path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return {}
        if signature != self._sent_times_signature:
            self._sent_times = dict(self.bus.last_sent_timestamps())
            self._sent_times_signature = signature
        canonical: dict[str, float] = {}
        for sender, timestamp in self._sent_times.items():
            name = self.registry.canonical_name(sender)
            canonical[name] = max(canonical.get(name, 0.0), timestamp)
        return canonical

    def full_history(self) -> Sequence[Message]:
        """Every message on the wire, in seq order (the combined view)."""
        return self._complete_history(self.full_history_page)

    def full_history_page(
        self,
        *,
        before: int | HistoryCursor | None = None,
        after: int | HistoryCursor | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        return self._integrated_display_page(
            self.bus.full_history_page,
            ChannelHistory(BuiltinChannel.ANY.value, BuiltinChannel.ANY.history_targets),
            worktree=None,
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
        return self._channel_views_for(
            snapshot,
            channels,
            catalog.pinned_members(),
            catalog.list_order,
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def _channel_views_for(
        self,
        snapshot: RegistrySnapshot,
        channels: Mapping[str, Channel],
        pins: Mapping[str, frozenset[str]],
        order: ChannelSort,
        *,
        show_stopped: bool,
        show_archived: bool,
        display_activity: Mapping[str, ChannelActivity] | None = None,
    ) -> tuple[ChannelView, ...]:
        people = {
            name: thread
            for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].in_view(
                show_stopped=show_stopped, show_archived=show_archived
            )
            and thread.role.executable
        }
        activity = self.agents.all_activity()
        sent = self.last_sent_timestamps()
        messages = self.bus.channel_activity() if display_activity is None else display_activity
        views: list[ChannelView] = []
        for channel in channels.values():
            pinned_members = pins.get(channel.name, frozenset())
            members = tuple(
                sorted(
                    (name for name, thread in people.items() if channel.matches(thread.tags)),
                    key=lambda name: (
                        name not in pinned_members,
                        channel.order.key(
                            name,
                            people[name].created_at,
                            activity[name].timestamp if name in activity else 0,
                            sent.get(name, 0),
                        ),
                    ),
                )
            )
            if display_activity is None:
                targets = (
                    channel.builtin.history_targets
                    if channel.builtin is not None
                    else frozenset({channel.name})
                )
                history = (
                    tuple(messages.values())
                    if targets is None
                    else tuple(messages.get(target, ChannelActivity()) for target in targets)
                )
            else:
                history = (messages.get(channel.name, ChannelActivity()),)
            views.append(
                ChannelView(
                    channel,
                    members,
                    max(
                        max((item.last_message for item in history), default=0),
                        max(
                            (activity[name].timestamp for name in members if name in activity),
                            default=0,
                        ),
                    ),
                    max((item.last_user_input for item in history), default=0),
                    pinned_members & frozenset(members),
                )
            )
        return tuple(sorted(views, key=order.key))

    def thread_views(
        self, *, show_stopped: bool = True, show_archived: bool = False
    ) -> tuple[ThreadView, ...]:
        return self._thread_views_for(
            self.registry.snapshot(), show_stopped=show_stopped, show_archived=show_archived
        )

    def _thread_views_for(
        self, snapshot: RegistrySnapshot, *, show_stopped: bool, show_archived: bool
    ) -> tuple[ThreadView, ...]:
        runtime = self.agents.runtime_info.read()
        active = frozenset(t.name for t in snapshot.threads.values() if t.executing)
        activities = self.agents.activity.all_current(active=active)
        waits = GoalWaits(self.root / GoalWaits.filename).read()
        return tuple(
            ThreadView(
                thread,
                snapshot.statuses[name],
                activities.get(name, Activity(name, ActivityState.IDLE, timestamp=0)),
                runtime.get(name),
                snapshot.last_seen.get(name, 0),
                GoalWaits.execution(thread.goal, waits, snapshot),
            )
            for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].in_view(
                show_stopped=show_stopped, show_archived=show_archived
            )
            and thread.role.executable
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
            self.thread_views(show_stopped=show_stopped, show_archived=show_archived),
            channels,
            unread,
            self.last_sent_timestamps(),
            channel_unread,
            catalog.list_order,
            show_stopped=show_stopped,
            show_archived=show_archived,
        )

    def viewer_snapshot(
        self, worktree: str, *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        """Local presentation scope, independent of agent delivery cursors."""
        viewer = self.messaging.user_identity(worktree).name
        with self._display_snapshot(viewer=viewer) as (basis, records, bus_revision):
            registry, declarations, scopes, order, captured_viewer, viewer_names, pins, notice = (
                basis
            )
            assert captured_viewer is not None
            display_activity, display_unread = self.presentation.display_view_metrics(
                records, scopes, scopes, captured_viewer, viewer_names, bus_revision
            )
            channels = self._channel_views_for(
                registry,
                declarations,
                pins,
                order,
                show_stopped=show_stopped,
                show_archived=show_archived,
                display_activity=display_activity,
            )
            threads = self._thread_views_for(
                registry, show_stopped=show_stopped, show_archived=show_archived
            )
            return CoordinationSnapshot(
                threads,
                channels,
                self.bus.pending_counts(captured_viewer),
                self.last_sent_timestamps(),
                display_unread,
                order,
                self.transcript_reads.counts(
                    captured_viewer,
                    {view.thread.name: view.thread.session_file or "" for view in threads},
                ),
                show_stopped=show_stopped,
                show_archived=show_archived,
                read_marker_notice=notice,
            )

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
        viewer = self.messaging.user_identity(worktree).name
        with _store_lock(self._wire_lock_path):
            revision = self._display_basis_revision()
            basis = self._capture_display_basis(viewer)
            current = next((scope for scope in basis[2] if scope.channel == target), None)
            if current is None:
                raise ValueError(f"Unknown channel: {target!r}")
            if through is None:
                # Explicit Mark Read selects the entire current view, unlike painted-page ACK.
                messages = (
                    message for message in self.bus.log.full_history() if current.includes(message)
                )
                displayed = self.bus.reads.capture(viewer, messages, basis[0], self.bus.log.path)
                through = self.bus.log.latest_sequence()
            else:
                if expected_scope is None or expected_scope.displayed is None:
                    raise ValueError("Channel display scope missing; refresh the displayed page.")
                if (
                    not current.same_projection(expected_scope)
                    or self._display_basis_revision() != revision
                ):
                    raise ValueError("Channel display changed; refresh the displayed page.")
                displayed = expected_scope.displayed
                displayed.validate(viewer, basis[0], self.bus.reads.bus_identity(self.bus.log.path))
            self.bus.mark_view_read(viewer, target, through, displayed=displayed)

    def mark_dm_view_read(
        self,
        peer: str,
        *,
        worktree: str,
        through: int,
        expected_display_basis: DMDisplayBasis,
    ) -> None:
        """CAS one painted human DM tail, never a global/executor ACK.

        The page must have no omitted unread inbound messages. The caller must
        additionally prove its through-bound was actually and contiguously
        painted; a fetched page alone cannot establish visibility.
        """
        proof = expected_display_basis
        if type(proof) is not DMDisplayBasis or type(through) is not int:
            raise ValueError("Painted DM read requires a typed page basis and integer bound.")
        with _store_lock(self._wire_lock_path), _store_lock(self.registry.store.path):
            snapshot = self.registry.store._read_unlocked().snapshot()
            proof.validate_for(
                self.root,
                worktree,
                peer,
                through,
                snapshot,
                self.bus.reads.bus_identity(self.bus.log.path),
            )
            self.bus.reads.mark_displayed(proof.viewer, proof.displayed.through(through))

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
                    *self.channels.catalog.source_paths(),
                    self.bus.log.path,
                    self.bus.history_manifest,
                    self.agents.activity._path,
                    self.agents.runtime_info.path,
                    self.bus.reads.path,
                    self.root / GoalWaits.filename,
                    self.bus.reads.path.with_name(self.bus.reads.legacy_filename),
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
        activities = self.agents.activity.all_current()
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
