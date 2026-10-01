"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .activity import Activity, ActivityState
from .audience_manifest import FrozenRecipient
from .bus_activity_index import ChannelActivity
from .bus_display_index import BusDisplayIndex, DisplayCheckpoint, DisplayMetricScope
from .bus_projection import BusFileRevision
from .channel_targets import is_channel_target
from .channels import Channel
from .display_order import ChannelSort
from .goal_presentation import GoalExecution
from .goal_waits import GoalWaits
from .mentions import MentionCandidate
from .message_page import MessagePage, MessagePageRequest
from .messages import Message
from .read_basis import ChannelDisplayScope, DMDisplayScope
from .read_ledger import ReadLedger
from .runtime_info import AgentRuntimeInfo
from .store_files import _store_lock, file_revision
from .thread_presentation import ThreadPresentation, ThreadOwnerBinding, UnavailableThreadOwnerBinding
from .thread_status import ThreadStatus
from .thread_identity import ThreadIncarnation
from .threads import Thread

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .agent_activity import AgentActivity
    from .catalog_document import CatalogDocument
    from .catalog_store import ChannelCatalog
    from .goal_waits import GoalWait
    from .message_bus import MessageBus
    from .messages import Message
    from .read_ledger import ReadLedger
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .wire_log import WireLog


@dataclass(frozen=True, slots=True)
class MessageNotification:
    """Read-only display of one recipient's durable assignment decision."""

    window_limit: ClassVar[int] = 120
    recipient_identity: FrozenRecipient
    state: str
    detail: str
    priority: int = 3
    busy: bool = False
    message: Message | None = None
    displayed_to: tuple[ThreadIncarnation, ...] = ()

    @property
    def recipient(self) -> str:
        return self.recipient_identity.canonical_thread

    @classmethod
    def window(
        cls, root: Path, registry: Registration, messages: Sequence[Message]
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Read one visible window's actual recipient outcomes; never schedule work.

        Display uses the coordinator's existing assignment decoder and lifecycle.
        A missing coordination store or absent row supplies no receipt. Errors remain
        visible to the caller instead of becoming false successful delivery.
        """
        from .notification_assignment import NotificationAssignment

        if len(messages) > cls.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = {(message.seq, message.message_id) for message in messages if message.seq > 0}
        result: dict[tuple[int, str], list[MessageNotification]] = {key: [] for key in keys}
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = NotificationAssignment.select(
            root, f"w.wire_seq IN ({placeholders})", tuple(key[0] for key in keys)
        )
        snapshot = registry.snapshot()
        owners = NotificationAssignment.active_owners(snapshot)
        from .agent_activity import AgentActivity

        activities = AgentActivity(root, registry).all_activity(snapshot=snapshot)
        reads = ReadLedger(root / ReadLedger.filename)
        document = reads.read()
        originals = {message.reference: message for message in messages}
        for receipt in rows:
            notification = receipt.project(owners, activities)
            source = receipt.assignment.source
            key = (source.seq, source.message_id)
            if key in result:
                result[key].append(
                    replace(
                        notification,
                        displayed_to=reads.displayed_recipient(
                            originals[source],
                            notification.recipient_identity,
                            snapshot,
                            document=document,
                        ),
                    )
                )
        return {key: tuple(rows) for key, rows in result.items()}

    @classmethod
    def recent(
        cls, root: Path, registry: Registration, log: WireLog, name: str, *, limit: int = 5
    ) -> tuple[MessageNotification, ...]:
        """Recent assigned messages, including channel work, for one agent view.

        Reads the original assignments and source messages. This is not a DM
        delivery or a read acknowledgement, and never schedules another turn.
        """
        if not 1 <= limit <= 20:
            raise ValueError("Recent notification limit must be between 1 and 20")
        owner = registry.require(name)
        from .transcript_receipts import AssignedTranscriptSource

        sources = AssignedTranscriptSource.for_thread(root, owner, log).rows(limit=limit)
        return cls.for_sources(root, registry, owner, sources)

    @classmethod
    def for_sources(
        cls, root: Path, registry: Registration, owner: Thread, sources: Sequence[CommittedDelivery]
    ) -> tuple[MessageNotification, ...]:
        """Project outcomes from the caller's original certified source window."""
        from .bus_publication import stable_thread_lookup

        lookup = stable_thread_lookup(owner.created_at)
        messages = tuple(source.message for source in sources)
        projected = cls.window(root, registry, messages)
        return tuple(
            replace(notification, message=source.message)
            for source in sources
            for notification in projected[source.message.seq, source.message.message_id]
            if source.audience.sender_lookup == lookup
            or notification.recipient_identity.recipient_lookup == lookup
        )


@dataclass(frozen=True, slots=True)
class DisplaySelection:
    """One captured registry/catalog/read basis for local human presentation."""

    registry: RegistrySnapshot
    catalog: CatalogDocument
    viewer: str | None
    seen: frozenset[int]
    notice: str | None

    @classmethod
    def capture(
        cls, registry: Registration, catalog: ChannelCatalog, reads: ReadLedger, viewer: str | None
    ) -> DisplaySelection:
        snapshot = registry.snapshot()
        canonical = snapshot.aliases.get(viewer, viewer) if viewer is not None else None
        seen = reads.seen_sequences(viewer, snapshot) if viewer is not None else frozenset()
        return cls(snapshot, catalog.read(), canonical, seen, reads.read().notice)

    @property
    def channels(self) -> Mapping[str, Channel]:
        return self.catalog.views(self.registry.threads)

    @property
    def scopes(self) -> tuple[ChannelDisplayScope, ...]:
        return tuple(
            ChannelDisplayScope.capture(channel, self.registry, seen=self.seen)
            for channel in self.channels.values()
        )

    @property
    def viewer_names(self) -> frozenset[str]:
        return (
            DMDisplayScope.names_for(self.viewer, self.registry)
            if self.viewer is not None
            else frozenset()
        )

    def scope(self, target: str) -> ChannelDisplayScope:
        channel = self.channels.get(target)
        if channel is None:
            raise ValueError(f"Unknown channel: {target!r}")
        return ChannelDisplayScope.capture(channel, self.registry, seen=self.seen)


class BusPresentation:
    def __init__(self, bus: MessageBus, registry: Registration, catalog: ChannelCatalog):
        self.bus = bus
        self.registry = registry
        self.catalog = catalog
        self._path = bus.log.path
        self._wire_lock_path = bus.log.path.parent / "wire"
        self._display_metrics: dict[str, DisplayCheckpoint] = {}

    def revision(self) -> tuple:
        """Revisions of every store used to define one display predicate and marker."""
        return tuple(
            file_revision(path)
            for path in (
                self.registry.store.path,
                self.catalog.path,
                self.bus.reads.path,
            )
        )

    @contextmanager
    def snapshot(
        self, viewer: str | None = None, target: str | None = None
    ) -> Iterator[tuple[DisplaySelection, Iterator[tuple[Message, int]], tuple | None]]:
        """Open one bus boundary atomically with a validated display basis.

        Direct registry/catalog writers need not hold the Comms wire lock; the
        revision checks detect their changes before the opened bus boundary.
        The raw scan happens after releasing that lock. A later append is not
        retried: the opened inode and byte limit already define a valid point.
        """
        for _ in range(3):
            with ExitStack() as stack:
                with _store_lock(self._wire_lock_path):
                    revision = self.revision()
                    basis = DisplaySelection.capture(
                        self.registry, self.catalog, self.bus.reads, viewer
                    )
                    if self.revision() != revision:
                        continue
                    bus_revision = file_revision(self.bus.log.path)
                    _, records = stack.enter_context(
                        self.bus.log._record_snapshot(need_sequence=False)
                    )
                    if self.revision() != revision:
                        continue
                    if file_revision(self.bus.log.path) != bus_revision:
                        bus_revision = None
                if target is not None and target not in basis.channels:
                    # A newly-created channel must not be rejected from an
                    # earlier basis without checking its current revision.
                    if self.revision() != revision:
                        continue
                    raise ValueError(f"Unknown channel: {target!r}")
                yield basis, records, bus_revision
                return
        raise RuntimeError("Display scope changed during snapshot; retry the request.")

    def channel_page(
        self,
        target: str,
        *,
        viewer: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Local display projection; underlying channel history remains target-owned."""
        if not is_channel_target(target):
            raise ValueError(f"{target!r} is not a channel target.")
        with self.snapshot(viewer=viewer, target=target) as (basis, records, _):
            scope = basis.scope(target)
            page = MessagePageRequest.capture(
                scope,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            ).collect(records)
            if viewer is not None:
                scope = replace(
                    scope,
                    displayed=self.bus.reads.capture(
                        viewer, page.messages, basis.registry, self.bus.log.path
                    ),
                )
            return replace(page, display_scope=scope)

    def mark_channel_read(
        self,
        target: str,
        *,
        viewer: str,
        through: int | None = None,
        expected_scope: ChannelDisplayScope | None = None,
    ) -> None:
        with _store_lock(self._wire_lock_path):
            revision = self.revision()
            basis = DisplaySelection.capture(self.registry, self.catalog, self.bus.reads, viewer)
            current = basis.scope(target)
            if through is None:
                # Explicit Mark Read selects the entire current view, unlike painted-page ACK.
                messages = (
                    message for message in self.bus.log.full_history() if current.includes(message)
                )
                displayed = self.bus.reads.capture(
                    viewer, messages, basis.registry, self.bus.log.path
                )
                through = self.bus.log.latest_sequence()
            else:
                if expected_scope is None:
                    raise ValueError("Channel display scope missing; refresh the displayed page.")
                displayed = expected_scope.require_displayed()
                if not current.same_projection(expected_scope) or self.revision() != revision:
                    raise ValueError("Channel display changed; refresh the displayed page.")
                displayed.validate(
                    viewer, basis.registry, self.bus.reads.bus_identity(self.bus.log.path)
                )
            self.bus.reads.mark_displayed(viewer, displayed.through(through))

    def display_view_metrics(
        self,
        records: Iterator[tuple[Message, int]],
        scopes: tuple[ChannelDisplayScope, ...],
        activity_scopes: tuple[ChannelDisplayScope, ...],
        viewer: str,
        viewer_names: frozenset[str],
        bus_revision: tuple[int, int, int, int] | None,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The caller supplies a canonical viewer and alias closure from the same
        captured registry as the scopes. Never recanonicalize during this scan.
        Caches may be reused or published only for an unchanged bus revision.
        """
        semantics = DisplayMetricScope(scopes, activity_scopes, viewer_names)
        cached = self._display_metrics.get(viewer)
        if bus_revision is not None and cached is not None:
            if cached.current_for(BusFileRevision(*bus_revision), semantics):
                return ({name: ChannelActivity(*clocks) for name, clocks in cached.activity.items()},
                        dict(cached.counts))
        initial = semantics.empty_metrics
        projected = BusDisplayIndex(self._path, viewer).snapshot(
            bus_revision, semantics, initial,
            lambda record, metrics: semantics.observe(Message.from_wire(record), metrics),
        )
        if projected is None:
            metrics = initial
            for message, _ in records:
                semantics.observe(message, metrics)
        else:
            metrics = projected.metrics
            if bus_revision is not None and file_revision(self._path) == bus_revision:
                self._display_metrics[viewer] = projected
        return ({name: ChannelActivity(*clocks) for name, clocks in metrics[0].items()},
                dict(metrics[1]))



@dataclass(frozen=True, slots=True)
class ChannelView:
    channel: Channel
    members: tuple[str, ...]
    last_activity: float = 0
    last_user_input: float = 0
    pinned_members: frozenset[str] = frozenset()

    @classmethod
    def roster(
        cls,
        snapshot: RegistrySnapshot,
        channels: Mapping[str, Channel],
        pins: Mapping[str, frozenset[str]],
        order: ChannelSort,
        activity: Mapping[str, Activity],
        sent: Mapping[str, float],
        messages: Mapping[str, ChannelActivity],
        *,
        show_stopped: bool,
        show_archived: bool,
    ) -> tuple[ChannelView, ...]:
        people = {
            name: thread
            for name, thread in snapshot.threads.items()
            if snapshot.statuses[name].in_view(
                show_stopped=show_stopped, show_archived=show_archived
            )
            and thread.role.executable
        }
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
            history = messages.get(channel.name, ChannelActivity())
            views.append(
                cls(
                    channel,
                    members,
                    max(
                        history.last_message,
                        max(
                            (activity[name].timestamp for name in members if name in activity),
                            default=0,
                        ),
                    ),
                    history.last_user_input,
                    pinned_members & frozenset(members),
                )
            )
        return tuple(sorted(views, key=order.key))

    def to_wire(self) -> dict[str, object]:
        return {
            **self.channel.to_wire(),
            "members": list(self.members),
            "last_activity": self.last_activity,
            "last_user_input": self.last_user_input,
            "pinned_members": sorted(self.pinned_members),
        }


@dataclass(frozen=True, slots=True)
class ThreadView:
    thread: Thread
    status: ThreadStatus
    activity: Activity
    runtime: AgentRuntimeInfo | None
    last_seen: float
    goal_execution: GoalExecution | None = None
    binding: ThreadOwnerBinding = UnavailableThreadOwnerBinding()

    @staticmethod
    def visible(
        thread: Thread, snapshot: RegistrySnapshot, *, show_stopped: bool, show_archived: bool
    ) -> bool:
        return thread.role.executable and snapshot.statuses[thread.name].in_view(
            show_stopped=show_stopped, show_archived=show_archived
        )

    @classmethod
    def capture(
        cls,
        thread: Thread,
        snapshot: RegistrySnapshot,
        activity: Activity,
        runtime: AgentRuntimeInfo | None,
        waits: dict[str, GoalWait],
    ) -> ThreadView:
        """Roster and individual readers join the same captured authorities."""
        return cls(
            thread,
            snapshot.statuses[thread.name],
            activity,
            runtime,
            snapshot.last_seen.get(thread.name, 0),
            GoalWaits.execution(thread.goal, waits, snapshot),
            snapshot.owner_binding(thread.name),
        )

    @classmethod
    def roster(
        cls,
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
        goal_waits: GoalWaits,
        *,
        show_stopped: bool,
        show_archived: bool,
    ) -> tuple[ThreadView, ...]:
        runtime = agents.runtime_info.read()
        activities = agents.all_activity(snapshot=snapshot)
        waits = goal_waits.read()
        return tuple(
            cls.capture(
                thread,
                snapshot,
                activities.get(name, Activity(name, ActivityState.IDLE, timestamp=0)),
                runtime.get(name),
                waits,
            )
            for name, thread in snapshot.threads.items()
            if cls.visible(thread, snapshot, show_stopped=show_stopped, show_archived=show_archived)
        )

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        return replace(self._display_presentation(), binding=self.binding)

    def _display_presentation(self) -> ThreadPresentation:
        if self.status.active and self.activity.diagnostic is not None:
            return self.activity.presentation(self.thread.title or self.thread.name)
        if self.status.active and self.thread.executing and not self.activity.state.busy:
            return ActivityState.WORKING.presentation(
                self.thread.title or self.thread.name, "In a turn"
            )
        if (
            self.status.active
            and not self.activity.state.busy
            and self.goal_execution is not None
            and self.goal_execution.state.view.waiting
        ):
            return self.goal_execution.presentation(self.thread.title or self.thread.name)
        return self.status.presentation(self.thread.title or self.thread.name, self.activity)

    def to_wire(self) -> dict[str, object]:
        return {
            **self.thread.to_wire(),
            "status": self.status.declared_name,
            "is_fork": self.thread.is_fork,
            "resumable": bool(self.thread.session_file),
            "last_seen": self.last_seen,
            "last_activity": self.activity.timestamp,
            "activity": self.activity.state.value,
            "activity_detail": self.activity.detail,
            "goal_execution": asdict(self.goal_execution) if self.goal_execution else None,
            "model": self.runtime.model if self.runtime else self.thread.model,
            "session_name": self.runtime.session_name if self.runtime else None,
            "context_used": self.runtime.context_used if self.runtime else None,
            "context_size": self.runtime.context_size if self.runtime else None,
            "context_percent": self.runtime.context_percent if self.runtime else None,
        }


@dataclass(frozen=True, slots=True)
class CoordinationSnapshot:
    threads: tuple[ThreadView, ...]
    channels: tuple[ChannelView, ...]
    unread: Mapping[str, int]
    last_sent: Mapping[str, float]
    channel_unread: Mapping[str, int] = field(default_factory=dict)
    channel_order: ChannelSort = ChannelSort.NAME
    thread_unread: Mapping[str, int] = field(default_factory=dict)
    thread_unread_pending: frozenset[str] = frozenset()
    show_stopped: bool = True
    show_archived: bool = False
    read_marker_notice: str | None = None

    def participants(self, channel: str) -> tuple[ThreadView, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        if view is None:
            return ()
        people = {
            person.thread.name: person
            for person in self.threads
            if person.status.active and person.thread.executing
        }
        return tuple(people[name] for name in view.members if name in people)

    def mention_candidates(self, channel: str) -> tuple[MentionCandidate, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        members = frozenset(view.members) if view else frozenset()
        return tuple(
            sorted(
                (
                    MentionCandidate(person.thread.name, person.presentation.title)
                    for person in self.threads
                    if person.thread.name in members
                ),
                key=lambda candidate: candidate.name.casefold(),
            )
        )


@dataclass(frozen=True, slots=True)
class WireRevision:
    files: tuple[tuple[int, int, int, int] | None, ...]
    expiry_tick: int
