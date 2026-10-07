"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .activity import ActivityState, ObservedActivity
from .audience_manifest import FrozenRecipient
from .bus_activity_index import ChannelActivity
from .bus_display_index import BusDisplayIndex, DisplayCheckpoint, DisplayMetricScope
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
    from .message_reference import MessageReference
    from .messages import Message
    from .read_ledger import ReadLedger
    from .read_ledger import ReadDocument
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .agent_activity import RecipientActivity
    from .wire_log import WireLog, OpenedWireSnapshot


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
        cls, root: Path, registry: Registration, log: WireLog, messages: Sequence[Message]
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Read one visible window's actual recipient outcomes; never schedule work.

        Display uses the coordinator's existing assignment decoder and lifecycle.
        A missing coordination store or absent row supplies no receipt. Errors remain
        visible to the caller instead of becoming false successful delivery.
        """
        if len(messages) > cls.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        references = tuple(reference for message in messages
                           for reference in message.notification_references())
        sources = log.deliveries_for_references(references)
        projected = cls.delivery_window(root, registry, sources)
        return {(message.seq,message.message_id):projected.get((message.seq,message.message_id), ())
                for message in messages}

    @classmethod
    def references(
        cls, root: Path, registry: Registration, log: WireLog,
        references: Sequence[MessageReference],
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Borrow mounted references in the original bounded delivery windows."""
        return cls.read_references(log, references, partial(cls.delivery_window, root, registry))

    @classmethod
    def read_references(
        cls, log: WireLog, references: Sequence[MessageReference],
        project: Callable[[Sequence[CommittedDelivery]], dict[tuple[int, str], tuple[MessageNotification, ...]]],
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Share bounded original delivery acquisition across live/recorded cuts."""
        result = {}
        for start in range(0, len(references), cls.window_limit):
            sources = log.deliveries_for_references(
                references[start : start + cls.window_limit]
            )
            result.update(project(sources))
        return result

    @classmethod
    def delivery_window(cls, root: Path, registry: Registration,
                        sources: Sequence[CommittedDelivery]):
        """Project the original frozen audience even before handling is recorded."""
        from .agent_activity import AgentActivity

        if not sources:
            return {}
        snapshot = registry.snapshot()
        observations = AgentActivity(root, registry).observe_recipients(
            (recipient for source in sources for recipient in source.audience.recipients),
            snapshot=snapshot,
        )
        return cls.project_delivery_window(root, snapshot, sources, observations)

    @classmethod
    def recorded_delivery_window(cls, root: Path, namespace: RegistryProvenance,
                                 sources: Sequence[CommittedDelivery]):
        from .agent_activity import RecordedRecipientActivity

        observations = {
            recipient.recipient_lookup: RecordedRecipientActivity()
            for source in sources for recipient in source.audience.recipients
        }
        return cls.project_delivery_window(root, namespace, sources, observations)

    @classmethod
    def project_delivery_window(
        cls, root: Path, snapshot: RegistryProvenance,
        sources: Sequence[CommittedDelivery], observations: Mapping[str, RecipientActivity],
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Original assignment and read authorities own both display projections."""
        from .notification_assignment import NotificationAssignment

        if len(sources) > cls.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = {(source.message.seq, source.message.message_id) for source in sources}
        result: dict[tuple[int, str], list[MessageNotification]] = {key: [] for key in keys}
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = NotificationAssignment.select(
            root, f"w.wire_seq IN ({placeholders})", tuple(key[0] for key in keys)
        )
        reads = ReadLedger(root / ReadLedger.filename)
        document = reads.read()
        for source, outcomes in NotificationAssignment.for_deliveries(sources, rows):
            key = (source.message.seq, source.message.message_id)
            for outcome in outcomes:
                observation = observations[outcome.recipient.recipient_lookup]
                notification = observation.after_inbox_read(
                    outcome.project(observation), source, reads, document, snapshot
                )
                result[key].append(
                    replace(
                        notification,
                        displayed_to=observation.displayed_recipient(
                            notification, source, reads, document, snapshot
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
        projected = cls.delivery_window(root, registry, sources)
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
        cls, snapshot: RegistrySnapshot, catalog: CatalogDocument,
        reads: ReadLedger, document: ReadDocument, viewer: str | None,
    ) -> DisplaySelection:
        canonical = snapshot.canonical_name(viewer) if viewer is not None else None
        seen = (
            reads.seen_sequences(viewer, snapshot, document=document)
            if viewer is not None else frozenset()
        )
        return cls(snapshot, catalog, canonical, seen, document.notice)

    @classmethod
    @contextmanager
    def reading(
        cls, registry: Registration, catalog: ChannelCatalog,
        reads: ReadLedger, viewer: str | None,
    ) -> Iterator[DisplaySelection]:
        """Borrow the original identity resources and read the ledger once.

        Read-ledger custody ends before yielding: painted acknowledgement may
        write that same ledger while identity and membership remain acquired.
        """
        with registry.store.reading() as document, catalog.reading() as channels:
            with reads.reading() as read_document:
                selected = cls.capture(
                    document.snapshot(), channels, reads, read_document, viewer
                )
            yield selected

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

    @contextmanager
    def snapshot(
        self, viewer: str | None = None, target: str | None = None
    ) -> Iterator[tuple[DisplaySelection, OpenedWireSnapshot]]:
        """Capture display inputs once through their original document resources.

        Open the bus before acquiring document resources: publication's lock
        order is bus then registry. Cross-store writers share wire; direct
        document writers cannot change the captured selection. Raw iteration
        holds none of these locks. Later appends are outside the opened cut;
        heartbeat changes cannot invalidate it.
        """
        with ExitStack() as stack:
            with _store_lock(self._wire_lock_path, shared=True):
                source = stack.enter_context(
                    self.bus.log._opened_wire_snapshot(need_sequence=False)
                )
                with DisplaySelection.reading(
                    self.registry, self.catalog, self.bus.reads, viewer
                ) as basis:
                    if target is not None:
                        basis.scope(target)
            yield basis, source

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
        with ExitStack() as resources:
            with _store_lock(self._wire_lock_path, shared=True):
                marker, _, source, stream, records = resources.enter_context(
                    self.bus.log.page_snapshot()
                )
                with DisplaySelection.reading(
                    self.registry, self.catalog, self.bus.reads, viewer
                ) as basis:
                    scope = basis.scope(target)
            # Identity capture does not grant custody over page preparation. Both
            # DM and channel pages borrow the same original reader and source cut.
            page = MessagePageRequest.capture(
                scope, before=before, after=after, limit=limit, max_bytes=max_bytes
            ).read_opened(self.bus.log, marker, source, stream, records)
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
        with ExitStack() as stack, _store_lock(self._wire_lock_path, shared=True):
            if through is None:
                # Acquire the bus cut before the registry, as publication does.
                records = stack.enter_context(self.bus.log.verified_snapshot())
            with DisplaySelection.reading(
                self.registry, self.catalog, self.bus.reads, viewer
            ) as basis:
                current = basis.scope(target)
                if through is None:
                    # Explicit Mark Read selects this entire opened source cut.
                    messages = (
                        message for record in records for message in record.messages()
                        if current.includes(message)
                    )
                    displayed = self.bus.reads.capture(
                        viewer, messages, basis.registry, self.bus.log.path
                    )
                else:
                    if expected_scope is None:
                        raise ValueError("Channel display scope missing; refresh the displayed page.")
                    displayed = expected_scope.require_displayed()
                    if not current.same_projection(expected_scope):
                        raise ValueError("Channel display changed; refresh the displayed page.")
                    displayed.validate(
                        viewer, basis.registry, self.bus.reads.bus_identity(self.bus.log.path)
                    )
                    displayed = displayed.through(through)
                self.bus.reads.mark_displayed(viewer, displayed)

    def display_view_metrics(
        self,
        source: OpenedWireSnapshot,
        scopes: tuple[ChannelDisplayScope, ...],
        activity_scopes: tuple[ChannelDisplayScope, ...],
        viewer: str,
        viewer_names: frozenset[str],
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The caller supplies a canonical viewer and alias closure from the same
        captured registry as the scopes. Never recanonicalize during this scan.
        Caches may be reused or published only for an unchanged bus revision.
        """
        semantics = DisplayMetricScope(scopes, activity_scopes, viewer_names)
        cached = self._display_metrics.get(viewer)
        if source.revision is not None and cached is not None:
            if cached.current_for(source.revision, semantics):
                return ({name: ChannelActivity(*clocks) for name, clocks in cached.activity.items()},
                        dict(cached.counts))
        initial = semantics.empty_metrics
        projected = BusDisplayIndex(self._path, viewer).snapshot(
            source, semantics,
        )
        if projected is None:
            metrics = initial
            for message, _ in source.public_records():
                semantics.observe(message, metrics)
        else:
            metrics = projected.metrics
            if file_revision(self._path) == (
                projected.source.inode, projected.source.size,
                projected.source.modified, projected.source.changed,
            ):
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
        activity: Mapping[str, ObservedActivity],
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
    activity: ObservedActivity
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
        activity: ObservedActivity,
        runtime: AgentRuntimeInfo | None,
        waits: dict[str, GoalWait],
    ) -> ThreadView:
        """Roster and individual readers join the same captured authorities."""
        return cls(
            thread,
            snapshot.statuses[thread.name],
            activity,
            runtime,
            snapshot.seen_at(thread.name),
            GoalWaits.execution(thread.goal, waits, snapshot),
            snapshot.owner_binding(thread.name),
        )

    @classmethod
    def roster(
        cls,
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
        activities: Mapping[str, ObservedActivity],
        goal_waits: GoalWaits,
        *,
        show_stopped: bool,
        show_archived: bool,
    ) -> tuple[ThreadView, ...]:
        runtime = agents.runtime_info.read()
        waits = goal_waits.read()
        return tuple(
            cls.capture(
                thread,
                snapshot,
                activities[name],
                runtime.get(name),
                waits,
            )
            for name, thread in snapshot.threads.items()
            if cls.visible(thread, snapshot, show_stopped=show_stopped, show_archived=show_archived)
        )

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        ordinary = self._display_presentation()
        if self.status.active:
            ordinary = self.activity.readiness.presentation(ordinary, busy=self.activity.state.busy)
        return replace(self.thread.execution.presentation(self.thread, self.status, ordinary),
                       binding=self.binding)

    def _display_presentation(self) -> ThreadPresentation:
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

    @classmethod
    def capture(
        cls, root: Path, registry: RegistrySnapshot, catalog: CatalogDocument,
        agents: AgentActivity, declarations: Mapping[str, Channel],
        sent: Mapping[str, float], messages: Mapping[str, ChannelActivity], *,
        unread: Mapping[str, int], channel_unread: Mapping[str, int],
        show_stopped: bool, show_archived: bool, read_marker_notice: str | None = None,
    ) -> CoordinationSnapshot:
        """Assemble both rosters from the caller's acquired identity and read cut."""
        activities = agents.all_activity(snapshot=registry)
        return cls(
            channels=ChannelView.roster(
                registry, declarations, catalog.pinned_members(), catalog.list_order,
                activities, sent, messages,
                show_stopped=show_stopped, show_archived=show_archived,
            ),
            threads=ThreadView.roster(
                registry, agents, activities, GoalWaits(root / GoalWaits.filename),
                show_stopped=show_stopped, show_archived=show_archived,
            ),
            unread=unread, last_sent=sent, channel_unread=channel_unread,
            channel_order=catalog.list_order, show_stopped=show_stopped,
            show_archived=show_archived, read_marker_notice=read_marker_notice,
        )

    @property
    def visible_channels(self) -> tuple[ChannelView, ...]:
        """Sidebar rows, distinct from still-open channel membership and history."""
        return tuple(view for view in self.channels
                     if view.channel.in_view(show_archived=self.show_archived))

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
