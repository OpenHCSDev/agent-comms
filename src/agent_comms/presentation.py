"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .activity import ActivityState, ObservedActivity
from .audience_manifest import FrozenRecipient
from .bus_activity_index import ChannelActivity
from .bus_display_index import BusDisplayIndex, DisplayMetricScope
from .channel_targets import is_channel_target
from .channels import Channel
from .display_order import ChannelSort, NameChannelSort
from .goal_presentation import GoalExecution
from .goal_waits import GoalWaits
from .mentions import MentionCandidate
from .message_page import ConversationMessagePageRequest, MessagePage, MessagePageRequest
from .message_reference import MessageReference
from .messages import Message
from .private_path import FileRevision
from .read_basis import ChannelDisplayScope, DMDisplayScope
from .read_ledger import (ReadLedger, ReadMask, ReadSource, AbsentReadSource,
                          ReadRevision, SelectedViewers, OneViewer, ReadNotice, NoReadNotice)
from .runtime_info import AgentRuntimeInfo
from .store_files import _store_lock
from .thread_identity import ThreadIncarnation
from .display_references import DisplayReferences
from .thread_presentation import (
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from .thread_status import ThreadStatus
from .threads import Thread

if TYPE_CHECKING:
    from .agent_activity import AgentActivity, RecipientActivity
    from .bus_publication import CommittedDelivery
    from .catalog_document import CatalogDocument
    from .catalog_store import ChannelCatalog
    from .goal_waits import GoalWait
    from .message_bus import MessageBus
    from .messages import Message
    from .notification_assignment import NotificationAssignmentRead
    from .read_ledger import ReadDocument, ReadLedger
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .wire_log import OpenedWireSnapshot, WireLog


@dataclass(frozen=True, slots=True)
class SentMessageOutcome:
    """A changed authored message outcome admitted once for prompt context."""

    message: Message
    recipient_identity: FrozenRecipient
    state: str
    detail: str
    answer: MessageReference | None = None

    @property
    def recipient(self) -> str:
        return self.recipient_identity.canonical_thread


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
    answer: MessageReference | None = None
    read_at_ms: int | None = None
    answered_at_ms: int | None = None
    changed: bool = False

    @property
    def recipient(self) -> str:
        return self.recipient_identity.canonical_thread

    def sent_outcome(self) -> SentMessageOutcome | None:
        if not self.changed or self.message is None:
            return None
        return SentMessageOutcome(
            self.message,
            self.recipient_identity,
            self.state,
            self.detail,
            self.answer,
        )

    @staticmethod
    def receipt_state(label: str, timestamp_ms: int | None) -> str:
        """Format a required durable receipt in the viewer's local time."""
        if timestamp_ms is None:
            raise ValueError(f"{label} notification requires a durable receipt time")
        recorded = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
        now = datetime.now().astimezone()
        when = recorded.strftime("%H:%M")
        if recorded.date() != now.date():
            when = recorded.strftime("%b %d %H:%M").replace(" 0", " ")
        return f"{label} {when}"

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
        projected = cls.references(root, registry, log, references)
        return {(message.seq,message.message_id):projected.get((message.seq,message.message_id), ())
                for message in messages}

    @classmethod
    def references(
        cls, root: Path, registry: Registration, log: WireLog,
        references: Sequence[MessageReference],
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Borrow mounted references in the original bounded delivery windows."""
        if not references:
            return {}
        from .notification_assignment import NotificationAssignment

        snapshot = registry.snapshot()
        return cls.read_references(
            log,
            references,
            lambda sources, identity: cls.delivery_window(
                root, registry, sources, NotificationAssignment.read(root, sources),
                snapshot=snapshot, identity=identity,
            ),
        )

    @classmethod
    def read_references(
        cls, log: WireLog, references: Sequence[MessageReference],
        project: Callable[[Sequence[CommittedDelivery], ReadSource], dict[tuple[int, str], tuple[MessageNotification, ...]]],
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Share bounded original delivery acquisition across live/recorded cuts."""
        result = {}
        for start in range(0, len(references), cls.window_limit):
            with log.certified_read() as captured:
                sources = tuple(captured.references(references[start:start + cls.window_limit]))
                identity = ReadSource.from_revision(captured.witness.revision)
            result.update(project(sources, identity))
        return result

    @classmethod
    def delivery_window(
        cls, root: Path, registry: Registration, sources: Sequence[CommittedDelivery],
        assignment_read: NotificationAssignmentRead, *, snapshot: RegistrySnapshot,
        identity: ReadSource, owner_lookup: str | None = None,
    ):
        """Project this window using the caller's one acquired namespace."""
        from .agent_activity import AgentActivity

        if not sources:
            return {}
        observations = AgentActivity(root, registry).observe_recipients(
            (recipient for source in sources for recipient in source.audience.recipients
             if cls.includes_recipient(source, recipient, owner_lookup)),
            snapshot=snapshot,
        )
        return cls.project_delivery_window(
            root, snapshot, sources, observations,
            owner_lookup=owner_lookup, assignment_read=assignment_read, identity=identity,
        )

    @staticmethod
    def includes_recipient(source: CommittedDelivery, recipient: FrozenRecipient,
                           owner_lookup: str | None) -> bool:
        """A sender sees all outcomes; a recipient sees only its original outcome."""
        return (owner_lookup is None or source.audience.sender_lookup == owner_lookup
                or recipient.recipient_lookup == owner_lookup)

    @classmethod
    def recorded_delivery_window(cls, root: Path, namespace: RegistryProvenance,
                                 sources: Sequence[CommittedDelivery], identity: ReadSource):
        from .agent_activity import RecordedRecipientActivity
        from .notification_assignment import NotificationAssignment

        observations = {
            recipient.recipient_lookup: RecordedRecipientActivity()
            for source in sources for recipient in source.audience.recipients
        }
        return cls.project_delivery_window(
            root, namespace, sources, observations,
            assignment_read=NotificationAssignment.read(root, sources), identity=identity,
        )

    @classmethod
    def project_delivery_window(
        cls, root: Path, snapshot: RegistryProvenance,
        sources: Sequence[CommittedDelivery], observations: Mapping[str, RecipientActivity],
        *, assignment_read: NotificationAssignmentRead, identity: ReadSource,
        owner_lookup: str | None = None,
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Original assignment and read authorities own both display projections."""
        from .notification_assignment import NotificationAssignment

        if len(sources) > cls.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = {(source.message.seq, source.message.message_id) for source in sources}
        result: dict[tuple[int, str], list[MessageNotification]] = {key: [] for key in keys}
        if not keys:
            return {}
        rows = assignment_read.resolve(root)
        reads = ReadLedger(root)
        document = reads.observations_for(snapshot, sources, identity)
        for source, outcomes in NotificationAssignment.for_deliveries(sources, rows):
            key = (source.message.seq, source.message.message_id)
            for outcome in outcomes:
                if not cls.includes_recipient(source, outcome.recipient, owner_lookup):
                    continue
                observation = observations[outcome.recipient.recipient_lookup]
                notification = observation.after_inbox_read(
                    outcome.project(observation), source, reads, document, snapshot
                )
                displayed_to = observation.displayed_recipient(
                    notification, source, reads, document, snapshot
                )
                result[key].append(
                    replace(
                        notification,
                        displayed_to=displayed_to,
                        changed=notification.changed or bool(displayed_to),
                    )
                )
            direct_read = cls.direct_read_notification(
                source, snapshot, reads, document, owner_lookup=owner_lookup
            )
            if direct_read is not None:
                result[key].append(direct_read)
        return {key: tuple(rows) for key, rows in result.items()}

    @classmethod
    def direct_read_notification(
        cls, source, snapshot, reads, document, *, owner_lookup
    ) -> MessageNotification | None:
        """Project a human DM read even though humans have no wake assignment."""
        from .bus_publication import stable_thread_lookup
        from .channel_targets import is_channel_target

        message = source.message
        if (
            source.audience.recipients
            or is_channel_target(message.target)
            or (owner_lookup is not None and source.audience.sender_lookup != owner_lookup)
        ):
            return None
        name = snapshot.canonical_name(message.target)
        target = snapshot.threads.get(name)
        if target is None or not reads.human(target.role):
            return None
        recipient = FrozenRecipient(stable_thread_lookup(target.created_at), target.name)
        displayed_to = reads.displayed_recipient(
            message, recipient, snapshot, readers=document
        )
        if not displayed_to:
            return None
        return cls(
            recipient,
            "Read",
            "",
            priority=2,
            displayed_to=displayed_to,
            changed=True,
        )

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

        captured = AssignedTranscriptSource.for_thread(root, owner, log).window(limit=limit)
        sources = captured.sources
        from .notification_assignment import NotificationAssignment

        return cls.for_sources(
            root, registry, owner, sources, identity=captured.read_source,
            assignment_read=NotificationAssignment.read(root, sources),
        )

    @classmethod
    def sent_sources(
        cls, root: Path, registry: Registration, log: WireLog, name: str, *, limit: int = 5
    ):
        """Capture one sender's bounded original source cut."""
        if not 1 <= limit <= cls.window_limit:
            raise ValueError("Sent notification limit must fit one bounded window")
        owner = registry.require(name)
        from .transcript_receipts import AssignedTranscriptSource

        captured = AssignedTranscriptSource.for_thread(root, owner, log).window(
            limit=0, sent_limit=limit
        )
        return owner, captured

    @classmethod
    def for_sources(
        cls, root: Path, registry: Registration, owner: Thread,
        sources: Sequence[CommittedDelivery], *, identity: ReadSource,
        assignment_read: NotificationAssignmentRead,
    ) -> tuple[MessageNotification, ...]:
        """Project outcomes from the caller's original certified source window."""
        if not sources:
            return ()
        from .bus_publication import stable_thread_lookup

        projected = cls.delivery_window(
            root, registry, sources,
            owner_lookup=stable_thread_lookup(owner.created_at),
            assignment_read=assignment_read,
            snapshot=registry.snapshot(), identity=identity,
        )
        return tuple(
            replace(notification, message=source.message)
            for source in sources
            for notification in projected[source.message.seq, source.message.message_id]
        )


@dataclass(frozen=True, slots=True)
class DisplaySelection:
    """One captured registry/catalog/read basis for local human presentation."""

    registry: RegistrySnapshot
    catalog: CatalogDocument
    viewer: str | None
    readers: dict[str, ReadMask]
    document: ReadDocument

    @property
    def seen(self) -> ReadMask:
        return self.readers.get(self.viewer, ReadMask())

    @classmethod
    def capture(
        cls, snapshot: RegistrySnapshot, catalog: CatalogDocument,
        document: ReadDocument, viewer: str | None, source: ReadSource,
    ) -> DisplaySelection:
        canonical = snapshot.canonical_name(viewer) if viewer is not None else None
        return cls(snapshot, catalog, canonical, source.memberships(document, snapshot), document)

    @classmethod
    @contextmanager
    def reading(
        cls, registry: Registration, catalog: ChannelCatalog,
        reads: ReadLedger, viewer: str | None, source: ReadSource,
    ) -> Iterator[DisplaySelection]:
        """Borrow the original identity resources and read the ledger once.

        Read-ledger custody ends before yielding: painted acknowledgement may
        write that same ledger while identity and membership remain acquired.
        """
        with registry.store.reading() as document, catalog.reading() as channels:
            snapshot = document.snapshot()
            viewers = (OneViewer(snapshot.participant(viewer).incarnation) if viewer is not None else
                       SelectedViewers(tuple(thread.incarnation for thread in snapshot.threads.values())))
            with reads.reading(viewers) as read_document:
                selected = cls.capture(snapshot, channels, read_document, viewer, source)
            yield selected

    @property
    def channels(self) -> Mapping[str, Channel]:
        return self.catalog.views(self.registry.threads)

    @property
    def scopes(self) -> tuple[ChannelDisplayScope, ...]:
        return tuple(
            ChannelDisplayScope.capture(channel, self.registry)
            for channel in self.channels.values()
        )

    @property
    def viewer_names(self) -> frozenset[str]:
        return (
            DMDisplayScope.names_for(self.viewer, self.registry)
            if self.viewer is not None
            else frozenset()
        )

    def require_viewer(self) -> str:
        if self.viewer is None:
            raise ValueError("Viewer metrics require a named viewer")
        return self.viewer

    def scope(self, target: str) -> ChannelDisplayScope:
        channel = self.channels.get(target)
        if channel is None:
            raise ValueError(f"Unknown channel: {target!r}")
        return ChannelDisplayScope.capture(channel, self.registry)


class BusPresentation:
    def __init__(
        self, bus: MessageBus, registry: Registration, catalog: ChannelCatalog,
        agents: AgentActivity,
    ):
        self.bus = bus
        self.registry = registry
        self.catalog = catalog
        self.agents = agents
        self._path = bus.log.path
        self._wire_lock_path = bus.log.path.parent / "wire"
        self._display_indexes: dict[str, BusDisplayIndex] = {}

    def display_index(self, viewer: str) -> BusDisplayIndex:
        index = self._display_indexes.get(viewer)
        if index is None:
            index = self.bus.log.register_append_index(BusDisplayIndex(self._path, viewer))
            self._display_indexes[viewer] = index
        return index

    @contextmanager
    def snapshot(self, viewer: str) -> Iterator[tuple[DisplaySelection, OpenedWireSnapshot]]:
        """Admit the display index before borrowing the bus-owned source cut."""
        self.display_index(viewer)
        with self.bus.snapshot(viewer) as selected:
            yield selected

    def roster(
        self, viewer: str, *, show_stopped: bool = True, show_archived: bool = False
    ) -> CoordinationSnapshot:
        """Current sidebar rows without advancing whole-bus derived indexes."""
        with DisplaySelection.reading(
            self.registry, self.catalog, self.bus.reads, viewer, AbsentReadSource()
        ) as basis:
            snapshot = CoordinationSnapshot.capture(
                self._path.parent, basis.registry, basis.catalog, self.agents, basis.channels,
                {}, {}, unread={}, channel_unread={}, show_stopped=show_stopped,
                show_archived=show_archived, read_marker_notice=basis.document.notice,
            )
        pending = {
            *(view.thread.name for view in snapshot.threads),
            *(view.channel.name for view in snapshot.channels),
        }
        snapshot = replace(
            snapshot, read_marker_notice=basis.document.notice.take(self.bus.reads)
        )
        return replace(snapshot, unread_pending=frozenset(pending))

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
                    self.registry, self.catalog, self.bus.reads, viewer, ReadSource.from_revision(source)
                ) as basis:
                    scope = basis.scope(target)
            # Identity capture does not grant custody over page preparation. Both
            # DM and channel pages borrow the same original reader and source cut.
            page = ConversationMessagePageRequest.capture(
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
            read_source = AbsentReadSource()
            if through is None:
                # Open the bus cut before the registry, as sending a message does.
                marker, _, source, stream, records = stack.enter_context(
                    self.bus.log.page_snapshot()
                )
                read_source = ReadSource.from_revision(source)
            with DisplaySelection.reading(
                self.registry, self.catalog, self.bus.reads, viewer, read_source
            ) as basis:
                current = basis.scope(target)
                if through is not None:
                    if expected_scope is None:
                        raise ValueError("Channel display scope missing; refresh the displayed page.")
                    displayed = expected_scope.require_displayed()
                    if current != expected_scope:
                        raise ValueError("Channel display changed; refresh the displayed page.")
                    displayed.validate(
                        viewer, basis.registry, self.bus.reads.bus_identity(self.bus.log.path)
                    )
                    self.bus.reads.mark_displayed(viewer, displayed.through(through), snapshot=basis.registry)
                    return
            # Explicit Mark Read selects the channel's messages in this whole
            # cut. The page index reads them once the registry is released,
            # because it takes the bus lock.
            page = MessagePageRequest.every(current).read_opened(
                self.bus.log, marker, source, stream, records
            )
            self.bus.reads.mark_displayed(
                viewer,
                self.bus.reads.capture(viewer, page.messages, basis.registry, self.bus.log.path),
                snapshot=basis.registry,
            )

    def display_view_metrics(
        self,
        source: OpenedWireSnapshot,
        selection: DisplaySelection,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The captured selection owns channel inclusion, viewer aliases and read
        membership. None is supplied independently by the metrics caller.
        Caches may be reused or published only for an unchanged bus revision.
        """
        semantics = DisplayMetricScope(selection.scopes, selection.viewer_names, selection.seen)
        initial = semantics.empty_metrics
        projected = self.display_index(selection.require_viewer()).snapshot(source, semantics)
        if projected is None:
            metrics = initial
            for message, _ in source.public_records():
                semantics.observe(message, metrics)
        else:
            metrics = projected.metrics
        return ({name: ChannelActivity(*clocks) for name, clocks in metrics[0].items()},
                dict(metrics[1]))



@dataclass(frozen=True, slots=True)
class ChannelView:
    channel: Channel
    members: tuple[str, ...]
    last_activity: float = 0
    last_user_input: float = 0
    pinned_members: frozenset[str] = frozenset()
    active_agents: int = 0
    registered_agents: int = 0

    @classmethod
    def roster(
        cls,
        snapshot: RegistrySnapshot,
        channels: Mapping[str, Channel],
        pins: Mapping[str, frozenset[str]],
        order: type[ChannelSort],
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
            if channel.archived and not show_archived:
                continue
            pinned_members = pins.get(channel.name, frozenset())
            registered = tuple(name for name, thread in snapshot.threads.items()
                               if thread.role.executable and channel.matches(thread.tags))
            members = tuple(
                sorted(
                    (name for name in registered if name in people),
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
                    sum(snapshot.statuses[name].active for name in registered),
                    len(registered),
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
        activities: Mapping[str, ObservedActivity] | None,
        goal_waits: GoalWaits,
        *,
        show_stopped: bool,
        show_archived: bool,
        names: Iterable[str] | None = None,
    ) -> tuple[ThreadView, ...]:
        selected = None if names is None else {snapshot.canonical_name(name) for name in names}
        threads = tuple(
            thread for name, thread in snapshot.threads.items()
            if (selected is None or name in selected)
            and cls.visible(thread, snapshot, show_stopped=show_stopped, show_archived=show_archived)
        )
        if not threads:
            return ()
        runtime = agents.runtime_info.read()
        waits = goal_waits.read()
        if activities is None:
            activities = agents.all_activity(snapshot=snapshot)
        return tuple(
            cls.capture(thread, snapshot, activities[thread.name], runtime.get(thread.name), waits)
            for thread in threads
        )

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        ordinary = self._display_presentation()
        if self.status.active:
            ordinary = self.activity.readiness.presentation(ordinary)
        return replace(self.thread.execution.presentation(self.thread, self.status, ordinary, self.binding),
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
    references: DisplayReferences
    channel_unread: Mapping[str, int] = field(default_factory=dict)
    channel_order: type[ChannelSort] = NameChannelSort
    thread_unread: Mapping[str, int] = field(default_factory=dict)
    unread_pending: frozenset[str] = frozenset()
    show_stopped: bool = True
    show_archived: bool = False
    read_marker_notice: ReadNotice = NoReadNotice()

    @classmethod
    def capture(
        cls, root: Path, registry: RegistrySnapshot, catalog: CatalogDocument,
        agents: AgentActivity, declarations: Mapping[str, Channel],
        sent: Mapping[str, float], messages: Mapping[str, ChannelActivity], *,
        unread: Mapping[str, int], channel_unread: Mapping[str, int],
        show_stopped: bool, show_archived: bool, read_marker_notice: ReadNotice = NoReadNotice(),
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
            references=DisplayReferences.capture(root, registry, declarations),
            channel_order=catalog.list_order, show_stopped=show_stopped,
            show_archived=show_archived, read_marker_notice=read_marker_notice,
        )

    def participants(self, channel: str) -> tuple[ThreadView, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        if view is None:
            return ()
        people = {
            person.thread.name: person
            for person in self.threads
            if person.status.active and person.presentation.status.busy
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
    """One observation of every Comms store a view derives from, by name."""

    registry: FileRevision | None
    catalog: FileRevision | None
    bus_log: FileRevision | None
    history: FileRevision | None
    activity: FileRevision | None
    runtime_info: FileRevision | None
    reads: ReadRevision
    native_unread: FileRevision | None
    goal_waits: FileRevision | None
    relationships: FileRevision | None
    notifications: tuple[FileRevision | None, ...]
    external_transcripts: tuple[FileRevision | None, ...]
    expiry_tick: int

    expiry_seconds: ClassVar[int] = 30

    @property
    def expiry_period(self) -> int:
        return self.expiry_tick // self.expiry_seconds

    def registrations_changed_since(self, earlier: WireRevision) -> bool:
        """A thread registration or a channel declaration changed."""
        return (self.registry, self.catalog) != (earlier.registry, earlier.catalog)

    def stores_changed_since(self, earlier: WireRevision) -> bool:
        """Any observed store changed; the activity expiry tick is not a store."""
        return replace(self, expiry_tick=earlier.expiry_tick) != earlier

    def status_changed_since(self, earlier: WireRevision) -> bool:
        """Activity, assignments and bus reads supply thread status.

        Draft and finished CLI items share one store. Neither changes the
        sidebar's roster, activity or unread bus counts. Their own observer
        still receives every revision and refreshes the conversation.
        """
        return replace(self, external_transcripts=earlier.external_transcripts,
                       native_unread=earlier.native_unread,
                       expiry_tick=earlier.expiry_tick) != earlier

    def sidebar_changed_since(self, earlier: WireRevision) -> bool:
        """Native unread progress updates counts without rebuilding thread presentations."""
        return self.native_unread != earlier.native_unread or self.status_changed_since(earlier)

    def external_transcripts_changed_since(self, earlier: WireRevision) -> bool:
        """CLI drafts and finished items changed, independently of coordination status."""
        return self.external_transcripts != earlier.external_transcripts

    def notifications_changed_since(self, earlier: WireRevision) -> bool:
        """Recipient state depends on assignments, presence, bus replies and read marks."""
        return replace(
            self, catalog=earlier.catalog, history=earlier.history,
            native_unread=earlier.native_unread, goal_waits=earlier.goal_waits,
            relationships=earlier.relationships, external_transcripts=earlier.external_transcripts,
            expiry_tick=earlier.expiry_tick,
        ) != earlier or self.expiry_period != earlier.expiry_period
