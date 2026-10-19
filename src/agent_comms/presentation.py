"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from . import relationships
from .activity import Activity, ActivityData, ActivityState, ObservedActivity
from .audience_manifest import FrozenRecipient
from .bus_activity_index import ChannelActivity
from .bus_display_index import BusDisplayIndex
from .channel_targets import CommandTarget
from .channels import Channel, ChannelMemberIndex, Channels
from .cli_result import CliResult
from .coordination_errors import DMPaintedBoundChanged, StaleRevision
from .display_order import ChannelSort, NameChannelSort
from .display_references import DisplayReferences
from .external_transcript import ExternalTranscript
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember, projected
from .goal_presentation import ThreadGoalExecution
from .goal_waits import GoalWaits
from .mentions import MentionCandidate
from .message_page import (
    NEWEST_PAGE,
    MessagePage,
    PageBounds,
)
from .message_reference import MessageReference
from .messages import Message
from .notification_assignment import NotificationAssignment
from .notification_progress import DeliveryProgress, HumanReadProgress, ReadDelivery
from .pi_counts import PiCount
from .private_path import FileRevision
from .read_basis import (AnonymousViewer, CapturedSelection, ChannelDisplayScope, DisplayViewer,
                         NamedViewer)
from .read_ledger import ReadLedger
from .recorded_bus import RecordedBusIdentity
from .registry_document import DEFAULT_ROSTER, RosterVisibility
from .runtime_info import RuntimeInfo
from .store_files import SharedLock, _store_lock
from .thread_identity import ThreadIncarnation
from .thread_presentation import (
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from .thread_status import ThreadStatus, ThreadStatusName
from .threads import Thread
from .tool_result import ToolResult
from .wire_log import WireLog, WireSnapshot

if TYPE_CHECKING:
    from .agent_activity import AgentActivity, RecipientActivity
    from .bus_publication import CommittedDelivery
    from .catalog_document import CatalogDocument
    from .catalog_store import ChannelCatalog
    from .comms import Comms
    from .goal_waits import RecordedGoalWait
    from .history_views import HistoryViews
    from .message_bus import MessageBus
    from .messages import Message
    from .notification_assignment import NotificationAssignmentRead
    from .read_ledger import ReadLedger
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .ui_model.observation import ThreadsObserved


class OutcomeViewer(ABC):
    """Who reads a delivery window decides which of its outcomes are shown."""

    @abstractmethod
    def includes(self, sender_lookup: str, recipient: FrozenRecipient) -> bool:
        pass

    @abstractmethod
    def sees_direct_read(self, source: CommittedDelivery) -> bool:
        pass


class AllOutcomes(OutcomeViewer):
    """An unscoped reader sees every recipient's outcome and every human read."""

    def includes(self, sender_lookup: str, recipient: FrozenRecipient) -> bool:
        return True

    def sees_direct_read(self, source: CommittedDelivery) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class OwnerOutcomes(OutcomeViewer):
    """A sender sees all outcomes; a recipient sees only its original outcome."""

    lookup: str

    @classmethod
    def of(cls, owner: Thread) -> OwnerOutcomes:
        return cls(owner.incarnation.lookup)

    def includes(self, sender_lookup: str, recipient: FrozenRecipient) -> bool:
        return self.lookup in (sender_lookup, recipient.recipient_lookup)

    def sees_direct_read(self, source: CommittedDelivery) -> bool:
        return source.audience.sender_lookup == self.lookup


@dataclass(frozen=True, slots=True)
class MessageNotification:
    """Read-only display of one recipient's durable assignment decision."""

    window_limit: ClassVar[int] = 120
    recipient_identity: FrozenRecipient
    sender_lookup: str
    state: DeliveryProgress
    message: Message | None = None
    displayed_to: tuple[ThreadIncarnation, ...] = ()
    answer: MessageReference | None = None
    answered_at_ms: int | None = None
    changed: bool = False

    @property
    def recipient(self) -> str:
        return self.recipient_identity.canonical_thread

    def activity_names(self) -> tuple[str, ...]:
        return (self.recipient, *(() if self.message is None else (self.message.sender,)))

    @property
    def label(self) -> str:
        return self.state.notification_label(self)

    def sent_outcome(self) -> SentMessageOutcome | None:
        if not self.changed or self.message is None:
            return None
        return SentMessageOutcome(self.recipient_identity, self.sender_lookup, self.state,
                                  message=self.message, answer=self.answer,
                                  answered_at_ms=self.answered_at_ms)

    @classmethod
    def ordered(cls, notifications: Iterable[MessageNotification]) -> tuple[MessageNotification, ...]:
        return tuple(sorted(notifications, key=lambda item: (item.state.priority, item.recipient.casefold())))

    @classmethod
    def summary(cls, notifications: Iterable[MessageNotification]) -> str:
        """One message's state and totals; clients never classify or count agents."""
        ordered = cls.ordered(notifications)
        if not ordered:
            return "No recorded notification result"
        delivered = sum(notification.state.delivered for notification in ordered)
        read = sum(notification.state.read for notification in ordered)
        total = len(ordered)
        return (f"{ordered[0].label} · Delivered {delivered}/{total}"
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
                root, registry, sources, NotificationAssignment.read(root, sources),
                viewer=AllOutcomes(),
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
        assignment_read: NotificationAssignmentRead, *, viewer: OutcomeViewer,
    ):
        """Project the original frozen audience even before handling is recorded."""
        from .agent_activity import AgentActivity

        if not sources:
            return {}
        snapshot = registry.snapshot()
        observations = AgentActivity(root, registry).observe_recipients(
            ((source, recipient) for source in sources for recipient in source.audience.recipients
             if viewer.includes(source.audience.sender_lookup, recipient)),
            snapshot=snapshot,
        )
        return cls.project_delivery_window(
            root, snapshot, sources, observations,
            viewer=viewer, assignment_read=assignment_read,
        )

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
            assignment_read=NotificationAssignment.read(root, sources), viewer=AllOutcomes(),
        )

    @classmethod
    def project_delivery_window(
        cls, root: Path, snapshot: RegistryProvenance,
        sources: Sequence[CommittedDelivery], observations: Mapping[str, RecipientActivity],
        *, assignment_read: NotificationAssignmentRead, viewer: OutcomeViewer,
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
        reads = ReadLedger.at(root)
        document = reads.read()
        for source, outcomes in NotificationAssignment.for_deliveries(sources, rows):
            key = (source.message.seq, source.message.message_id)
            for outcome in outcomes:
                if not viewer.includes(source.audience.sender_lookup, outcome.recipient):
                    continue
                observation = observations[outcome.recipient.recipient_lookup]
                notification = observation.after_inbox_read(
                    outcome.project(observation, source.audience.sender_lookup), source, reads, document, snapshot
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
                source, snapshot, reads, document, viewer=viewer
            )
            if direct_read is not None:
                result[key].append(direct_read)
        return {key: tuple(rows) for key, rows in result.items()}

    @classmethod
    def direct_read_notification(
        cls, source, snapshot, reads, document, *, viewer: OutcomeViewer
    ) -> MessageNotification | None:
        """Project a human DM read even though humans have no wake assignment."""
        message = source.message
        if source.audience.recipients or not viewer.sees_direct_read(source):
            return None
        peer = CommandTarget.named(message.target).direct_peer(snapshot)
        for target in () if peer is None else peer.role.human(peer):
            recipient = FrozenRecipient(target.incarnation.lookup, target.name)
            displayed_to = reads.displayed_recipient(
                source, recipient, snapshot, document=document
            )
            if not displayed_to:
                return None
            return cls(
                recipient,
                source.audience.sender_lookup,
                HumanReadProgress(),
                displayed_to=displayed_to,
                changed=True,
            )
        return None

    @classmethod
    def sent_sources(
        cls, root: Path, registry: Registration, log: WireLog, name: str, *, limit: int = 5
    ):
        """Capture one sender's bounded original source cut."""
        if not 1 <= limit <= cls.window_limit:
            raise ValueError("Sent notification limit must fit one bounded window")
        owner = registry.require(name)
        from .transcript_sources import AssignedTranscriptSource

        captured = AssignedTranscriptSource.for_thread(root, owner, log).window(
            limit=0, sent_limit=limit
        )
        return owner, captured.sent_sources

    @classmethod
    def for_sources(
        cls, root: Path, registry: Registration, viewer: OutcomeViewer,
        sources: Sequence[CommittedDelivery], *,
        assignment_read: NotificationAssignmentRead,
    ) -> tuple[MessageNotification, ...]:
        """Project outcomes from the caller's original certified source window."""
        projected = cls.delivery_window(
            root, registry, sources, assignment_read, viewer=viewer,
        )
        return tuple(
            replace(notification, message=source.message)
            for source in sources
            for notification in projected[source.message.seq, source.message.message_id]
        )


@dataclass(frozen=True, slots=True)
class SentMessageOutcome(MessageNotification):
    """The same typed outcome, with its original message required for context."""
    message: Message = field(kw_only=True)


class ChannelReadMark(ABC):
    """A read request carries its own source cut and displayed-message selection."""

    def apply(self, presentation: BusPresentation, target: str, viewer: str) -> None:
        with _store_lock(presentation._wire_lock_path, mode=SharedLock()):
            with self.reading(presentation, target, viewer) as displayed:
                presentation.bus.reads.mark_displayed(displayed.viewer, displayed)

    @abstractmethod
    def reading(self, presentation: BusPresentation, target: str, viewer: str): ...


@dataclass(frozen=True, slots=True)
class ReadAll(ChannelReadMark):
    @contextmanager
    def reading(self, presentation, target, viewer):
        with presentation.bus.log.opened_wire_snapshot() as source:
            with CapturedSelection.reading(
                presentation.registry, presentation.catalog, presentation.bus.reads,
                NamedViewer(viewer),
            ) as basis:
                scope = basis.scope(target)
            yield source.query_page_index(lambda index: scope.capture_all(
                presentation.bus.reads, basis.require_viewer(), basis.registry, source, index,
                document=basis.document,
            ))


@dataclass(frozen=True, slots=True)
class ReadThrough(ChannelReadMark):
    through: int
    expected_scope: ChannelDisplayScope

    @contextmanager
    def reading(self, presentation, target, viewer):
        with CapturedSelection.reading(
            presentation.registry, presentation.catalog, presentation.bus.reads,
            NamedViewer(viewer),
        ) as basis:
            displayed = self.expected_scope.require_displayed()
            if basis.scope(target) != self.expected_scope:
                raise DMPaintedBoundChanged()
            displayed.validate(
                basis.require_viewer(), basis.registry, RecordedBusIdentity.capture(presentation.bus.log.path),
            )
            yield displayed.through(self.through)


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
    def snapshot(self, viewer: NamedViewer) -> Iterator[tuple[CapturedSelection, WireSnapshot]]:
        """Check the display index before borrowing the bus-owned source cut."""
        self.display_index(viewer.name)
        with self.bus.snapshot(viewer) as selected:
            yield selected

    def roster(
        self, viewer: str, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> CoordinationSnapshot:
        """Current sidebar rows without advancing whole-bus derived indexes."""
        with CapturedSelection.reading(
            self.registry, self.catalog, self.bus.reads, NamedViewer(viewer)
        ) as basis:
            snapshot = CoordinationSnapshot.capture(
                self._path.parent, basis.registry, basis.catalog, self.agents, basis.channels,
                {}, {}, unread={}, channel_unread={}, visibility=visibility,
                read_marker_notice=basis.document.notice,
            )
        return replace(
            snapshot, read_marker_notice=self.bus.reads.take_notice(basis.document.notice),
            unread_pending=frozenset(snapshot.threads.keys() | snapshot.channels.keys()),
        )

    def channel_page(
        self,
        target: str,
        *,
        viewer: DisplayViewer = AnonymousViewer(),
        bounds: PageBounds = NEWEST_PAGE,
    ) -> MessagePage:
        """Local display projection; underlying channel history remains target-owned."""
        target = CommandTarget.named(target).require_channel()
        with ExitStack() as resources:
            with _store_lock(self._wire_lock_path, mode=SharedLock()):
                source = resources.enter_context(self.bus.log.opened_wire_snapshot())
                with CapturedSelection.reading(
                    self.registry, self.catalog, self.bus.reads, viewer
                ) as basis:
                    scope = basis.scope(target.name)
            # Reading identity is separate from preparing pages. Both
            # DM and channel pages borrow the same original reader and source cut.
            return basis.viewer.read_page(scope, bounds, basis, source)

    def mark_channel_read(
        self,
        target: str,
        *,
        viewer: str,
        mark: ChannelReadMark,
    ) -> None:
        mark.apply(self, target, viewer)

    def display_view_metrics(
        self,
        source: WireSnapshot,
        basis: CapturedSelection,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The captured basis owns channel inclusion, viewer aliases and read
        membership. None is supplied independently by the metrics caller.
        The index reads the captured complete-line cut; read and write errors propagate.
        """
        snapshot = self.display_index(basis.require_viewer()).snapshot(source, basis.scopes)
        unread = source.query_page_index(lambda page: page.display_unread(
            basis.scopes, basis.viewer_names, basis.seen(source), source.complete_prefix.boundary, basis.registry))
        return snapshot.activity, unread



@dataclass(frozen=True, slots=True)
class ChannelView(ToolResult):
    """Wire form: the channel's own fields beside its roster; agent counts stay in process."""
    channel: Channel = field(metadata={"wire_inline": True})
    members: tuple[str, ...]
    last_activity: float = 0
    last_user_input: float = 0
    pinned_members: frozenset[str] = frozenset()
    active_agents: int = field(default=0, metadata={"wire_exclude": True})
    registered_agents: int = field(default=0, metadata={"wire_exclude": True})

    @classmethod
    def roster(cls, snapshot: RegistrySnapshot, channels: Iterable[Channel],
        pins: Mapping[str, frozenset[str]], order: type[ChannelSort],
        activity: Mapping[str, ObservedActivity], sent: Mapping[str, float],
        messages: Mapping[str, ChannelActivity], visibility: RosterVisibility,
    ) -> tuple[ChannelView, ...]:
        index = ChannelMemberIndex(snapshot.threads.values())
        return tuple(sorted((cls.capture(
            channel, channel.members(index), snapshot,
            pins.get(channel.name, frozenset()), activity, sent,
            messages.get(channel.name, ChannelActivity()), visibility,
        ) for channel in channels), key=order.key))

    @classmethod
    def capture(
        cls, channel: Channel, registered: tuple[Thread, ...], snapshot: RegistrySnapshot,
        pins: frozenset[str], activity: Mapping[str, ObservedActivity], sent: Mapping[str, float],
        history: ChannelActivity, visibility: RosterVisibility,
    ) -> ChannelView:
        people = {thread.name: thread for thread in registered
                  if snapshot.statuses[thread.name].in_view(visibility)}
        members = tuple(sorted(people, key=lambda name: (
            name not in pins,
            channel.order.key(name, people[name].created_at,
                              activity[name].timestamp if name in activity else 0,
                              sent.get(name, 0)),
        )))
        return cls(
            channel, members,
            max(history.last_message,
                max((activity[name].timestamp for name in members if name in activity), default=0)),
            history.last_user_input, pins & frozenset(members),
            sum(snapshot.statuses[thread.name].active for thread in registered), len(registered),
        )



@dataclass(frozen=True)
class InboxCount(DeclaredFamily, JsonShapeFamily, affix="Inbox"):
    """A thread's unread count as its reader took it: counted, or not counted at all."""


@dataclass(frozen=True)
class CountedInbox(InboxCount, JsonShapeMember):
    value: int


@dataclass(frozen=True)
class UncountedInbox(InboxCount, JsonShapeMember):
    value: None = None


@dataclass(frozen=True)
class InboxRead(DeclaredFamily, affix="Read"):
    """Whether a thread read counts each thread's unread messages."""

    @abstractmethod
    def counts(self, bus, names: Sequence[str]) -> Mapping[str, InboxCount]: ...


@dataclass(frozen=True)
class CountedRead(InboxRead):
    def counts(self, bus, names: Sequence[str]) -> Mapping[str, InboxCount]:
        # One bus snapshot for every thread, not one per thread.
        return {name: CountedInbox(count) for name, count in bus.pending_counts_all(names).items()}


@dataclass(frozen=True)
class UncountedRead(InboxRead):
    def counts(self, bus, names: Sequence[str]) -> Mapping[str, InboxCount]:
        return {name: UncountedInbox() for name in names}


@dataclass(frozen=True, slots=True)
class ThreadRecord(CliResult):
    """A thread's wire record; every reader of one thread writes these fields from here."""
    thread: Thread = field(metadata={"wire_inline": True})
    status: Annotated[ThreadStatus, ThreadStatusName]
    activity: ActivityData = field(metadata={"presence_exclude": True})
    goal_execution: ThreadGoalExecution
    pending: InboxCount = field(
        default=UncountedInbox(), kw_only=True, metadata={"wire_omit_default": True})

    @classmethod
    def read(cls, snapshot: RegistrySnapshot, agents: AgentActivity,
             waits: dict[str, RecordedGoalWait], threads: Iterable[Thread],
             pending: Mapping[str, InboxCount]) -> tuple[ThreadRecord, ...]:
        """Only active threads are observed; the rest show the absent activity."""
        threads = tuple(threads)
        observed = agents.all_activity(
            (thread for thread in threads if snapshot.statuses[thread.name].active),
            snapshot=snapshot)
        return tuple(cls(
            thread, snapshot.statuses[thread.name],
            observed.get(thread.name, Activity.absent(thread.name)),
            GoalWaits.execution(thread.goal, waits, snapshot, agents),
            pending=pending[thread.name],
        ) for thread in threads)

    def encoded(self) -> object:
        return FieldCodec.project(self, "presence")

    @projected(view="presence")
    def is_fork(self) -> bool:
        return self.thread.is_fork

    @projected(view="presence", name="activity")
    def activity_state(self) -> ActivityState:
        return self.activity.state

    @projected(view="presence")
    def activity_detail(self) -> str:
        return self.activity.detail


@dataclass(frozen=True, slots=True)
class ThreadView(ThreadRecord):
    activity: ObservedActivity = field(metadata={"presence_exclude": True})
    runtime: RuntimeInfo = field(metadata={"presence_exclude": True})
    last_seen: float
    binding: ThreadOwnerBinding = field(
        default=UnavailableThreadOwnerBinding(), metadata={"presence_exclude": True})

    @classmethod
    def roster(
        cls, snapshot: RegistrySnapshot, agents: AgentActivity,
        goal_waits: GoalWaits,
        visibility: RosterVisibility, *, names: Iterable[str] | None = None,
    ) -> tuple[ThreadView, ...]:
        threads = tuple(snapshot.visible_threads(visibility, names=names))
        if not threads:
            return ()
        runtime = agents.runtime_info.for_threads(thread.name for thread in threads)
        waits = goal_waits.read()
        activities = agents.all_activity(threads, snapshot=snapshot)
        return tuple(cls(
            thread, snapshot.statuses[thread.name], activities[thread.name],
            GoalWaits.execution(thread.goal, waits, snapshot, agents), runtime[thread.name],
            snapshot.seen_at(thread.name), snapshot.owner_binding(thread.name),
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
        if self.thread.turn_record.read().executing:
            return ActivityState.WORKING.presentation(ordinary.title, "In a turn")
        return self.goal_execution.waiting_presentation(ordinary)

    @projected(view="presence")
    def resumable(self) -> bool:
        return self.thread.runtime.resumable

    @projected(view="presence")
    def last_activity(self) -> float:
        return self.activity.timestamp

    @projected(view="presence")
    def session_name(self) -> str | None:
        return self.runtime.session_name

    @projected(view="presence")
    def context_used(self) -> PiCount:
        return self.runtime.context_used

    @projected(view="presence")
    def context_size(self) -> PiCount:
        return self.runtime.context_size



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
    read_marker_notice: str | None = None

    @classmethod
    def capture(
        cls, root: Path, registry: RegistrySnapshot, catalog: CatalogDocument,
        agents: AgentActivity, declarations: Channels,
        sent: Mapping[str, float], messages: Mapping[str, ChannelActivity], *,
        unread: Mapping[str, int], channel_unread: Mapping[str, int],
        visibility: RosterVisibility, read_marker_notice: str | None = None,
    ) -> CoordinationSnapshot:
        """Assemble both rosters from the caller's acquired identity and read cut."""
        threads = OrderedDict((view.thread.name, view) for view in ThreadView.roster(
            registry, agents, GoalWaits.at(root), visibility,
        ))
        activities = {name: view.activity for name, view in threads.items()}
        return cls(
            channels=OrderedDict((view.channel.name, view) for view in ChannelView.roster(
                registry, declarations.visible(visibility), catalog.pinned_members(), catalog.list_order,
                activities, sent, messages, visibility,
            )),
            threads=threads,
            unread=unread, last_sent=sent, channel_unread=channel_unread,
            references=DisplayReferences.capture(root, registry, declarations),
            channel_order=catalog.list_order, read_marker_notice=read_marker_notice,
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


class ReadState:
    __slots__ = ()

    def _changed_since(self, earlier: WireRevision) -> bool:
        return True

    bus_source_changed_since = _changed_since
    _registrations_changed_from = _changed_since
    stores_changed_since = _changed_since
    status_changed_since = _changed_since
    sidebar_changed_since = _changed_since
    external_transcripts_changed_since = _changed_since
    notifications_changed_since = _changed_since

    def registrations_changed_since(self, earlier: ReadState) -> bool:
        return True


class NoRevision(ReadState):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class WireRevision(ReadState):
    """One observation of every Comms store a view derives from, by name."""

    registry: tuple[FileRevision, ...] = revision_source(lambda views: (views.registry.store.path,))
    catalog: tuple[FileRevision, ...] = revision_source(lambda views: (views.channels.catalog.path,))
    bus_log: tuple[FileRevision, ...] = revision_source(lambda views: (views.bus.log.path,))
    history: tuple[FileRevision, ...] = revision_source(lambda views: (views.bus.history.path,))
    activity: tuple[FileRevision, ...] = revision_source(lambda views: (views.agents.activity._path,))
    runtime_info: tuple[FileRevision, ...] = revision_source(lambda views: (views.agents.runtime_info.path,))
    reads: tuple[FileRevision, ...] = revision_source(lambda views: (views.bus.reads.path,))
    native_unread: tuple[FileRevision, ...] = revision_source(lambda views: (views.transcript_reads.index_path,))
    goal_waits: tuple[FileRevision, ...] = revision_source(lambda views: (GoalWaits.path_in(views.root),))
    relationships: tuple[FileRevision, ...] = revision_source(lambda views: (relationships.RelationshipStore.path_in(views.root),))
    notifications: tuple[FileRevision, ...] = revision_source(lambda views: NotificationAssignment.handling_paths(views.root))
    external_transcripts: tuple[FileRevision, ...] = revision_source(lambda views: ExternalTranscript.source_paths(views.root))
    expiry_tick: int

    expiry_seconds: ClassVar[int] = 30

    @classmethod
    def empty(cls) -> WireRevision:
        return cls(**{source.name: () for source in fields(cls) if "paths" in source.metadata},
                   expiry_tick=0)

    @classmethod
    def source_paths(cls, views: HistoryViews) -> dict[str, tuple[Path, ...]]:
        """Stat and watch the sources declared by the revision fields themselves."""
        return {source.name: paths(views) for source in fields(cls)
                if (paths := source.metadata.get("paths")) is not None}

    @classmethod
    def capture(cls, views: HistoryViews, *, expiry_tick: int) -> WireRevision:
        """Stat sources only; strict content reads belong to the requesting view."""
        captured = {
            name: tuple(revision for path in group
                        if (revision := FileRevision.capture(path)) is not None)
            for name, group in cls.source_paths(views).items()
        }
        return cls(**captured, expiry_tick=expiry_tick)

    def changed_threads(self, previous: ThreadsObserved, comms: Comms,
                        activity: Mapping[str, ObservedActivity]) -> frozenset[str]:
        """Attribute changes using the retained observation and existing route rows."""
        earlier = previous.revision
        names = previous.results.keys()
        readable, changed = previous.revision_inputs()
        if (self.registry != earlier.registry or self.bus_source_changed_since(earlier)
                or not readable):
            return frozenset(names)
        if self.bus_log != earlier.bus_log:
            snapshot = comms.registry.snapshot()
            with comms.bus.log.opened_wire_snapshot() as source:
                if source.changed_since(self.bus_log[0]):
                    raise StaleRevision("Wire source changed before route attribution")
                actors = {actor for message, _ in source.public_records(earlier.bus_log[0].size)
                          for actor in (message.sender, message.target)}
            catalog = comms.channels.catalog.read()
            for actor in actors:
                changed.update(CommandTarget.named(actor).changed_threads(names, snapshot, catalog))
            changed.update(alias for alias, target in snapshot.aliases.items() if target in changed)
        activity_changed = {name for name in activity
                            if activity[name] != previous.activity.get(name)}
        status_names = names if (self.goal_waits, self.expiry_period) != (
            earlier.goal_waits, earlier.expiry_period) else names & activity_changed
        if status_names:
            status_batch = comms.views.thread_presentations(status_names)
            changed.update(name for name in status_names
                           if (status_view := status_batch.view(name)) is None
                           or not previous.results[name].same_status(status_view.presentation))
        notifications_changed = self.notifications_changed_since(earlier)
        remaining = {name: view for name, view in readable.items() if name not in changed
                     and (notifications_changed
                          or view.notification_activity_names() & activity_changed)}
        references = tuple(dict.fromkeys(reference for view in remaining.values()
                                        for reference in (*view.incoming_references, *view.sent_references)))
        if references:
            outcomes = MessageNotification.references(comms.root, comms.registry, comms.bus.log, references)
            for name, view in remaining.items():
                viewer = OwnerOutcomes.of(view.read_identity.thread)
                current = tuple(replace(item, message=None)
                                for reference in (*view.incoming_references, *view.sent_references)
                                for item in outcomes[reference.seq, reference.message_id]
                                if viewer.includes(item.sender_lookup, item.recipient_identity))
                before = tuple(replace(item, message=None)
                               for item in (*view.notifications, *view.sent_notifications))
                if current != before:
                    changed.add(name)
        return frozenset(changed & names)

    def bus_source_changed_since(self, earlier: WireRevision) -> bool:
        return any(
            FileRevision.source_changed(before, current)
            for before, current in zip_longest(earlier.bus_log, self.bus_log))

    @property
    def expiry_period(self) -> int:
        return self.expiry_tick // self.expiry_seconds

    def registrations_changed_since(self, earlier: ReadState) -> bool:
        return earlier._registrations_changed_from(self)

    def _registrations_changed_from(self, earlier: WireRevision) -> bool:
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
        """Whole-window changes; activity is attributed to its message participants."""
        return (self.registry, self.reads, self.notifications) != (
            earlier.registry, earlier.reads, earlier.notifications
        ) or self.expiry_period != earlier.expiry_period
