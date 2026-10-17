"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .activity import ActivityState, ObservedActivity
from .coordination_errors import StaleRevision
from .audience_manifest import FrozenRecipient
from .bus_activity_index import ChannelActivity
from .bus_route_counts import OpenedRouteCounts
from .bus_display_index import BusDisplayIndex, DisplayMetricScope
from .channel_targets import is_channel_target
from .channels import Channel
from .display_order import ChannelSort, NameChannelSort
from .goal_presentation import GoalExecution
from .goal_waits import GoalWaits
from .external_transcript import ExternalTranscript
from .notification_assignment import NotificationAssignment
from .relationships import RelationshipStore
from .mentions import MentionCandidate
from .message_page import ConversationMessagePageRequest, MessagePage, MessagePageRequest
from .message_reference import MessageReference
from .notification_progress import DeliveryProgress, HumanReadProgress, ReadDelivery
from .messages import Message
from .private_path import FileRevision
from .read_basis import ChannelDisplayScope
from .read_ledger import ReadLedger
from .runtime_info import AgentRuntimeInfo
from .store_files import _store_lock, _store_read_lock
from .wire_log import OpenedWireSnapshot, WireLog
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
    from .comms import Comms
    from .ui_model.observation import ThreadsObserved
    from .bus_publication import CommittedDelivery
    from .catalog_document import CatalogDocument
    from .catalog_store import ChannelCatalog
    from .history_views import HistoryViews
    from .message_bus import MessageBus
    from .messages import Message
    from .notification_assignment import NotificationAssignmentRead
    from .read_ledger import ReadDocument, ReadLedger
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance


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
    progress: DeliveryProgress = field(kw_only=True)
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

    @classmethod
    def ordered(cls, notifications: Iterable[MessageNotification]) -> tuple[MessageNotification, ...]:
        return tuple(sorted(notifications, key=lambda item: (item.priority, item.recipient.casefold())))

    @classmethod
    def summary(cls, notifications: Iterable[MessageNotification]) -> str:
        """One message's state and totals; clients never classify or count agents."""
        ordered = cls.ordered(notifications)
        if not ordered:
            return "No recorded notification result"
        delivered = sum(notification.progress.delivered for notification in ordered)
        read = sum(notification.progress.read for notification in ordered)
        total = len(ordered)
        return (f"{ordered[0].state} · Delivered {delivered}/{total}"
                f" · {ReadDelivery.label} {read}/{total}")

    @staticmethod
    def timed_state(label: str, timestamp_ms: int | None) -> str:
        """Format a recorded event in the viewer's local time."""
        if timestamp_ms is None:
            raise ValueError(f"{label} notification requires a recorded time")
        recorded = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
        now = datetime.now().astimezone()
        when = recorded.strftime("%H:%M")
        if recorded.date() != now.date():
            when = recorded.strftime("%b %d %H:%M").replace(" 0", " ")
        return f"{label} {when}"

    @classmethod
    def references(
        cls, root: Path, registry: Registration, log: WireLog,
        references: Sequence[MessageReference],
    ) -> dict[tuple[int, str], tuple[MessageNotification, ...]]:
        """Borrow mounted references in the original bounded delivery windows."""
        from .notification_assignment import NotificationAssignment

        return cls.read_references(
            log,
            references,
            lambda sources: cls.delivery_window(
                root, registry, sources, NotificationAssignment.read(root, sources)
            ),
        )

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
    def delivery_window(
        cls, root: Path, registry: Registration, sources: Sequence[CommittedDelivery],
        assignment_read: NotificationAssignmentRead, *, owner_lookup: str | None = None,
    ):
        """Project the original frozen audience even before handling is recorded."""
        from .agent_activity import AgentActivity

        if not sources:
            return {}
        snapshot = registry.snapshot()
        observations = AgentActivity(root, registry).observe_recipients(
            ((source, recipient) for source in sources for recipient in source.audience.recipients
             if cls.includes_recipient(source, recipient, owner_lookup)),
            snapshot=snapshot,
        )
        return cls.project_delivery_window(
            root, snapshot, sources, observations,
            owner_lookup=owner_lookup, assignment_read=assignment_read,
        )

    @staticmethod
    def includes_recipient(source: CommittedDelivery, recipient: FrozenRecipient,
                           owner_lookup: str | None) -> bool:
        """A sender sees all outcomes; a recipient sees only its original outcome."""
        return (owner_lookup is None or source.audience.sender_lookup == owner_lookup
                or recipient.recipient_lookup == owner_lookup)

    @classmethod
    def recorded_delivery_window(cls, root: Path, namespace: RegistryProvenance,
                                 sources: Sequence[CommittedDelivery]):
        from .agent_activity import RecordedRecipientActivity
        from .notification_assignment import NotificationAssignment

        observations = {
            recipient.recipient_lookup: RecordedRecipientActivity()
            for source in sources for recipient in source.audience.recipients
        }
        return cls.project_delivery_window(
            root, namespace, sources, observations,
            assignment_read=NotificationAssignment.read(root, sources),
        )

    @classmethod
    def project_delivery_window(
        cls, root: Path, snapshot: RegistryProvenance,
        sources: Sequence[CommittedDelivery], observations: Mapping[str, RecipientActivity],
        *, assignment_read: NotificationAssignmentRead,
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
        reads = ReadLedger(root / ReadLedger.filename)
        document = reads.read()
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
        recipient = FrozenRecipient(target.incarnation.lookup, target.name)
        displayed_to = reads.displayed_recipient(
            source, recipient, snapshot, document=document
        )
        if not displayed_to:
            return None
        return cls(
            recipient,
            HumanReadProgress.label,
            "",
            priority=2,
            displayed_to=displayed_to,
            progress=HumanReadProgress(),
            changed=True,
        )

    @classmethod
    def sent_sources(
        cls, root: Path, registry: Registration, log: WireLog, name: str, *, limit: int = 5
    ):
        """Capture one sender's bounded original source cut."""
        if not 1 <= limit <= cls.window_limit:
            raise ValueError("Sent notification limit must fit one bounded window")
        owner = registry.require(name)
        from .transcript_wire import AssignedTranscriptSource

        captured = AssignedTranscriptSource.for_thread(root, owner, log).window(
            limit=0, sent_limit=limit
        )
        return owner, captured.sent_sources

    @classmethod
    def for_sources(
        cls, root: Path, registry: Registration, owner: Thread,
        sources: Sequence[CommittedDelivery], *,
        assignment_read: NotificationAssignmentRead,
    ) -> tuple[MessageNotification, ...]:
        """Project outcomes from the caller's original certified source window."""
        projected = cls.delivery_window(
            root, registry, sources,
            owner_lookup=owner.incarnation.lookup,
            assignment_read=assignment_read,
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
    seen: frozenset[int]
    document: ReadDocument

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
        return cls(snapshot, catalog, canonical, seen, document)

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
            ChannelDisplayScope.capture(channel, self.registry)
            for channel in self.channels.values()
        )

    @property
    def viewer_names(self) -> frozenset[str]:
        return (
            self.registry.names_for(self.viewer)
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
            self.registry, self.catalog, self.bus.reads, viewer
        ) as basis:
            snapshot = CoordinationSnapshot.capture(
                self._path.parent, basis.registry, basis.catalog, self.agents, basis.channels,
                {}, {}, unread={}, channel_unread={}, show_stopped=show_stopped,
                show_archived=show_archived, read_marker_notice=basis.document.notice,
            )
        return replace(
            snapshot, read_marker_notice=self.bus.reads.take_notice(basis.document.notice),
            unread_pending=frozenset(snapshot.threads.keys() | snapshot.channels.keys()),
        )

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
                source = resources.enter_context(self.bus.log._opened_wire_snapshot())
                with DisplaySelection.reading(
                    self.registry, self.catalog, self.bus.reads, viewer
                ) as basis:
                    scope = basis.scope(target)
            # Identity capture does not grant custody over page preparation. Both
            # DM and channel pages borrow the same original reader and source cut.
            page = ConversationMessagePageRequest.capture(
                scope, before=before, after=after, limit=limit, max_bytes=max_bytes
            ).read_opened(source)
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
                # Open the bus cut before the registry, as sending a message does.
                source = stack.enter_context(self.bus.log._opened_wire_snapshot())
            with DisplaySelection.reading(
                self.registry, self.catalog, self.bus.reads, viewer
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
                    self.bus.reads.mark_displayed(viewer, displayed.through(through))
                    return
            # Explicit Mark Read selects the channel's messages in this whole
            # cut. The page index reads them once the registry is released,
            # because it takes the bus lock.
            page = MessagePageRequest.every(current).read_opened(source)
            self.bus.reads.mark_displayed(
                viewer,
                self.bus.reads.capture(viewer, page.messages, basis.registry, self.bus.log.path),
            )

    def display_view_metrics(
        self,
        source: OpenedWireSnapshot,
        selection: DisplaySelection,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The captured selection owns channel inclusion, viewer aliases and read
        membership. None is supplied independently by the metrics caller.
        The index reads the captured complete-line cut; read and write errors propagate.
        """
        semantics = DisplayMetricScope(selection.scopes, selection.viewer_names, selection.seen)
        metrics = self.display_index(selection.require_viewer()).snapshot(source, semantics).metrics
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
        people = {thread.name: thread for thread in snapshot.visible_threads(
            show_stopped=show_stopped, show_archived=show_archived,
        )}
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

    @classmethod
    def roster(
        cls, snapshot: RegistrySnapshot, agents: AgentActivity,
        goal_waits: GoalWaits,
        *, show_stopped: bool, show_archived: bool, names: Iterable[str] | None = None,
    ) -> tuple[ThreadView, ...]:
        threads = tuple(snapshot.visible_threads(
            names=names, show_stopped=show_stopped, show_archived=show_archived,
        ))
        if not threads:
            return ()
        runtime, waits = agents.runtime_info.read(), goal_waits.read()
        activities = agents.all_activity(threads, snapshot=snapshot)
        return tuple(cls(
            thread, snapshot.statuses[thread.name], activities[thread.name], runtime.get(thread.name),
            snapshot.seen_at(thread.name), GoalWaits.execution(thread.goal, waits, snapshot),
            snapshot.owner_binding(thread.name),
        ) for thread in threads)

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        ordinary = self._display_presentation()
        if self.status.active:
            ordinary = self.activity.readiness.presentation(ordinary)
        return replace(self.thread.execution.presentation(self.thread, self.status, ordinary, self.binding),
                       binding=self.binding)

    def _display_presentation(self) -> ThreadPresentation:
        ordinary = self.status.presentation(self.thread.display_title, self.activity)
        if self.status.active:
            return ordinary.status.when_idle(ordinary, self._idle_presentation)
        return ordinary

    def _idle_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        if self.thread.executing:
            return ActivityState.WORKING.presentation(ordinary.title, "In a turn")
        if self.goal_execution is not None and self.goal_execution.state.view.waiting:
            return self.goal_execution.presentation(ordinary.title)
        return ordinary

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
            "goal_execution": self.goal_execution.to_wire() if self.goal_execution else None,
            "session_name": self.runtime.session_name if self.runtime else None,
            "context_used": self.runtime.context_used if self.runtime else None,
            "context_size": self.runtime.context_size if self.runtime else None,
            "context_percent": self.runtime.context_percent if self.runtime else None,
        }


@dataclass(frozen=True, slots=True)
class CoordinationSnapshot:
    threads: Mapping[str, ThreadView]
    channels: Mapping[str, ChannelView]
    unread: Mapping[str, int]
    last_sent: Mapping[str, float]
    references: DisplayReferences
    channel_unread: Mapping[str, int] = field(default_factory=dict)
    channel_order: type[ChannelSort] = NameChannelSort
    thread_unread: Mapping[str, int] = field(default_factory=dict)
    unread_pending: frozenset[str] = frozenset()
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
        threads = OrderedDict((view.thread.name, view) for view in ThreadView.roster(
            registry, agents, GoalWaits(root / GoalWaits.filename),
            show_stopped=show_stopped, show_archived=show_archived,
        ))
        activities = {name: view.activity for name, view in threads.items()}
        return cls(
            channels=OrderedDict((view.channel.name, view) for view in ChannelView.roster(
                registry, declarations, catalog.pinned_members(), catalog.list_order,
                activities, sent, messages,
                show_stopped=show_stopped, show_archived=show_archived,
            )),
            threads=threads,
            unread=unread, last_sent=sent, channel_unread=channel_unread,
            references=DisplayReferences.capture(root, registry, declarations),
            channel_order=catalog.list_order, show_stopped=show_stopped,
            show_archived=show_archived, read_marker_notice=read_marker_notice,
        )

    def participants(self, channel: str) -> tuple[ThreadView, ...]:
        view = self.channels.get(channel)
        if view is None:
            return ()
        return tuple(
            person for name in view.members
            if (person := self.threads.get(name)) is not None and person.status.active
            and person.presentation.status.working
        )

    def mention_candidates(self, channel: str) -> tuple[MentionCandidate, ...]:
        view = self.channels.get(channel)
        if view is None:
            return ()
        return tuple(
            sorted(
                (
                    MentionCandidate(name, person.presentation.title)
                    for name in view.members
                    if (person := self.threads.get(name)) is not None
                ),
                key=lambda candidate: candidate.name.casefold(),
            )
        )


def revision_source(paths: Callable[[HistoryViews], tuple[Path, ...]]):
    return field(metadata={"paths": paths})


@dataclass(frozen=True, slots=True)
class WireRevision:
    """One observation of every Comms store a view derives from, by name."""

    registry: tuple[FileRevision, ...] = revision_source(lambda views: (views.registry.store.path,))
    catalog: tuple[FileRevision, ...] = revision_source(lambda views: (views.channels.catalog.path,))
    bus_log: tuple[FileRevision, ...] = revision_source(lambda views: (views.bus.log.path,))
    history: tuple[FileRevision, ...] = revision_source(lambda views: (views.bus.history.path,))
    activity: tuple[FileRevision, ...] = revision_source(lambda views: (views.agents.activity._path,))
    runtime_info: tuple[FileRevision, ...] = revision_source(lambda views: (views.agents.runtime_info.path,))
    reads: tuple[FileRevision, ...] = revision_source(lambda views: (views.bus.reads.path,))
    native_unread: tuple[FileRevision, ...] = revision_source(lambda views: (views.transcript_reads.index_path,))
    goal_waits: tuple[FileRevision, ...] = revision_source(lambda views: (views.root / GoalWaits.filename,))
    relationships: tuple[FileRevision, ...] = revision_source(lambda views: (views.root / RelationshipStore.filename,))
    notifications: tuple[FileRevision, ...] = revision_source(lambda views: NotificationAssignment.handling_paths(views.root))
    external_transcripts: tuple[FileRevision, ...] = revision_source(lambda views: ExternalTranscript.source_paths(views.root))
    expiry_tick: int
    through_seq: int

    expiry_seconds: ClassVar[int] = 30

    @classmethod
    def source_paths(cls, views: HistoryViews) -> dict[str, tuple[Path, ...]]:
        """Stat and watch the sources declared by the revision fields themselves."""
        return {source.name: paths(views) for source in fields(cls)
                if (paths := source.metadata.get("paths")) is not None}

    @classmethod
    def capture(cls, views: HistoryViews, *, expiry_tick: int) -> WireRevision:
        """Every source is a group; missing files contribute no revision."""
        paths = cls.source_paths(views)
        with _store_read_lock(views.bus.log.path) as source:
            through_seq = views.bus.log._committed_sequence_unlocked(source)
            captured = {
                name: tuple(revision for path in group
                            if (revision := FileRevision.capture(path)) is not None)
                for name, group in paths.items()
            }
        return cls(**captured, expiry_tick=expiry_tick, through_seq=through_seq)

    def changed_threads(self, previous: ThreadsObserved, comms: Comms) -> frozenset[str]:
        """Attribute changes using the retained view's originals and existing route rows."""
        earlier = previous.revision
        names = previous.presentations.keys() | previous.unavailable.keys() | previous.busy
        if (self.registry != earlier.registry or self.bus_source_changed_since(earlier)
                or not any(view is not None for view in previous.presentations.values())):
            return frozenset(names)
        changed = set(previous.busy)
        if self.through_seq > earlier.through_seq:
            snapshot = comms.registry.snapshot()
            with comms.bus.log._opened_wire_snapshot() as source:
                if (source.revision,) != self.bus_log:
                    raise StaleRevision("Wire source changed before route attribution")
                with OpenedRouteCounts(comms.bus.log.path, source, snapshot, {}) as routes:
                    actors = routes.participants_between(earlier.through_seq, self.through_seq)
            channels = tuple(comms.channels.catalog.read().resolve(name)
                             for name in actors if is_channel_target(name))
            changed.update(snapshot.canonical_name(name) for name in actors if not is_channel_target(name))
            changed.update(name for name in names if (thread := snapshot.threads.get(name)) is not None
                           and any(channel.matches(thread.tags) for channel in channels))
            changed.update(alias for alias, target in snapshot.aliases.items() if target in changed)
        if (self.activity, self.goal_waits, self.expiry_period) != (
            earlier.activity, earlier.goal_waits, earlier.expiry_period
        ):
            batch = comms.views.thread_presentations(names)
            changed.update(name for name in names if (view := batch.view(name)) is None
                           or not view.presentation.same_status(previous.presentations.get(name)))
        if self.notifications_changed_since(earlier):
            remaining = {name: view for name, view in previous.presentations.items()
                         if name not in changed and view is not None}
            references = tuple(dict.fromkeys(reference for view in remaining.values()
                                            for reference in (*view.incoming_references, *view.sent_references)))
            if references:
                outcomes = MessageNotification.references(comms.root, comms.registry, comms.bus.log, references)
                for name, view in remaining.items():
                    lookup = view.read_identity.thread.incarnation.lookup
                    current = tuple(replace(item, message=None)
                                    for reference in (*view.incoming_references, *view.sent_references)
                                    for item in outcomes[reference.seq, reference.message_id]
                                    if reference in view.sent_references or item.recipient_identity.recipient_lookup == lookup)
                    before = tuple(replace(item, message=None)
                                   for item in (*view.notifications, *view.sent_notifications))
                    if current != before:
                        changed.add(name)
        return frozenset(changed & names)

    def bus_source_changed_since(self, earlier: WireRevision) -> bool:
        return self.through_seq < earlier.through_seq or any(
            FileRevision.source_changed(before, current)
            for before, current in zip_longest(earlier.bus_log, self.bus_log))

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
        return (self.registry, self.activity, self.reads, self.notifications) != (
            earlier.registry, earlier.activity, earlier.reads, earlier.notifications
        ) or self.expiry_period != earlier.expiry_period
