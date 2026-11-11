"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from agent_comms.messaging.display_sources import ChannelSortView
from agent_comms.messaging.notification_contracts import RevisionChanges

from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Annotated, ClassVar, Self

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.coordination.errors import DMPaintedBoundChanged
from agent_comms.display_order import ChannelSort, NameChannelSort
from agent_comms.execution.runtime_info import RuntimeInfo
from agent_comms.execution.transcript_sources import ExternalTranscript
from agent_comms.messaging.activity_clocks import ChannelActivity
from agent_comms.messaging.bus import BusSnapshot, DisplayUnread
from agent_comms.messaging.bus_read_source import BusReadSource
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.channels import Channel, ChannelMemberIndex, Channels
from agent_comms.messaging.display_references import CurrentDisplayReferences
from agent_comms.messaging.mentions import MentionCandidate
from agent_comms.messaging.message_notification import MessageNotification
from agent_comms.messaging.message_page import NEWEST_PAGE, MessagePage, PageBounds
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.read_basis import (
    AnonymousViewer,
    ChannelDisplayScope,
    DisplayViewer,
    NamedViewer,
)
from agent_comms.messaging.read_ledger import NoReadNotice, ReadNotice
from agent_comms.messaging.read_selection import CapturedSelection
from agent_comms.messaging.recorded_bus import NoRecordedBus, RecordedBusIdentity
from agent_comms.messaging.revision_views import (
    ActivityView,
    BusSourceView,
    ExternalTranscriptsView,
    NotificationEvidenceView,
    NotificationsView,
    RegistrationsView,
    RevisionView,
    SidebarView,
    StatusSourced,
    StoresView,
    TeamBoardView,
    TeamFramesView,
)
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    Differs,
    FieldCodec,
    JsonShapeFamily,
    JsonShapeMember,
    PresenceExclude,
    WatchPaths,
    WatchViews,
    WireExclude,
    WireInline,
    WireOmitDefault,
    projected,
)
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.pi_counts import PiCount
from agent_comms.planning.goal_execution import GoalExecution
from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot
from agent_comms.planning.goal_snapshots import GoalSnapshots
from agent_comms.planning.goal_waits import GoalWaits
from agent_comms.planning.orders import TodoStore
from agent_comms.registry.activity import Activity, ActivityData, ActivityState, ObservedActivity
from agent_comms.registry.agent_activity import ActivityAnswers, AgentActivity, CapturedActivity
from agent_comms.registry.catalog_document import CatalogDocument
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.relationship_file import RelationshipFile
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER, RosterVisibility
from agent_comms.registry.thread_presentation import (
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from agent_comms.registry.thread_status import ActiveThreadPresence, ThreadStatus, ThreadStatusName
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import GoalWakeFile
from agent_comms.storage.private_path import FileRevision
from agent_comms.storage.storage_room import RoomLedger
from agent_comms.team.team_frame_file import TeamFrameFile
from agent_comms.transport.tool_result import ToolResult
from agent_comms.ui_model.unread import (
    ExactUnread,
    IndexingUnread,
    UnobservedUnread,
    UnreadPresentation,
)


@dataclass(frozen=True, slots=True)
class SentMessageSummary:
    """One sent message's recipient handling: its original reference and the totals
    MessageNotification.summary reports. comms_message_outcomes reads each recipient's
    own outcome for that reference, so the message is never repeated per recipient."""

    reference: MessageReference
    summary: str

    @classmethod
    def for_sources(
        cls,
        sources: Iterable[tuple[MessageReference, Iterable[MessageNotification]]],
    ) -> tuple[Self, ...]:
        """One summary per sent source, aggregated by MessageNotification.summary."""
        return tuple(
            cls(reference, MessageNotification.summary(outcomes)) for reference, outcomes in sources
        )


class ChannelReadMark(ABC):
    """A read request carries its own source cut and displayed-message selection."""

    def apply(self, presentation: BusPresentation, target: str, viewer: str) -> None:
        with self.reading(presentation, target, viewer) as displayed:
            presentation.bus.reads.mark_displayed(displayed.viewer, displayed)

    @abstractmethod
    def reading(self, presentation: BusPresentation, target: str, viewer: str): ...


@dataclass(frozen=True, slots=True)
class ReadAll(ChannelReadMark):
    @contextmanager
    def reading(self, presentation, target, viewer):
        with presentation.snapshot(NamedViewer(viewer)) as (basis, source):
            scope = basis.scope(target)
            yield scope.capture_all(
                presentation.bus.reads, basis.require_viewer(), basis.registry, source,
                document=basis.document, through=source.revision(),
            )


@dataclass(frozen=True, slots=True)
class ReadThrough(ChannelReadMark):
    through: int
    expected_scope: ChannelDisplayScope

    @contextmanager
    def reading(self, presentation, target, viewer):
        with presentation.bus.opened_activity() as activity:
            basis = CapturedSelection.load(
                presentation.registry, presentation.catalog, presentation.bus.reads,
                NamedViewer(viewer), activity,
            )
            displayed = self.expected_scope.require_displayed()
            if basis.scope(target) != self.expected_scope:
                raise DMPaintedBoundChanged()
            displayed.validate(
                basis.require_viewer(), basis.registry, RecordedBusIdentity.capture(presentation.bus.bus.path),
            )
            yield displayed.through(self.through)


class BusPresentation:
    def __init__(
        self, bus: BusReadSource, registry: Registration, catalog: ChannelCatalog,
        agents: AgentActivity, goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers],
    ):
        self.bus = bus
        self.registry = registry
        self.catalog = catalog
        self.agents = agents
        self.goals = goals
        self._path = bus.bus.path
        self._display_unread = DisplayUnread()

    @contextmanager
    def snapshot(self, viewer: NamedViewer) -> Iterator[tuple[CapturedSelection, BusSnapshot]]:
        """Borrow the bus-owned source cut without preparing a second store."""
        with self.bus.snapshot(viewer) as selected:
            yield selected

    def roster(
        self, viewer: str, visibility: RosterVisibility = DEFAULT_ROSTER,
    ) -> CoordinationSnapshot:
        """Current sidebar rows without advancing whole-bus derived indexes."""
        with self.bus.opened_activity() as activity:
            basis = CapturedSelection.load(
                self.registry, self.catalog, self.bus.reads, NamedViewer(viewer), activity,
            )
            snapshot = CoordinationSnapshot.capture(
                self._path.parent, basis.registry, basis.catalog, self.agents, basis.channels,
                {}, {}, goals=self.goals, unread={}, channel_unread={}, visibility=visibility,
                read_marker_notice=basis.document.notice,
            )
        return replace(
            snapshot, read_marker_notice=basis.document.notice.take(self.bus.reads),
            unread={name: IndexingUnread() for name in snapshot.threads},
            channel_unread={name: IndexingUnread() for name in snapshot.channels},
        )

    def channel_page(
        self,
        target: str,
        *,
        viewer: DisplayViewer = AnonymousViewer(),
        bounds: PageBounds = NEWEST_PAGE,
    ) -> MessagePage:
        """Local display projection; underlying channel history remains target-owned."""
        channel = CommandTarget.named(target).require_channel()
        with self.bus.snapshot(viewer) as (basis, source):
            return basis.viewer.read_page(basis.scope(channel.name), bounds, basis, source)

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
        source: BusSnapshot,
        basis: CapturedSelection,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The captured basis owns channel inclusion, viewer aliases and read
        membership. None is supplied independently by the metrics caller.
        Derived maxima are committed by the append, never prepared by this reader.
        """
        selection = basis.unread_selection(source)
        activity = {scope.channel: scope.activity(source) for scope in selection.scopes}
        counted = self._display_unread.advanced(source, selection)
        self._display_unread = counted
        return activity, counted.counts


@dataclass(frozen=True, slots=True)
class ChannelView(ToolResult, ChannelSortView[type[ChannelSort]]):
    """Wire form: the channel's own fields beside its roster; agent counts stay in process."""

    channel: Annotated[Channel, WireInline()]
    members: tuple[str, ...]
    last_activity: float = 0
    last_user_input: float = 0
    pinned_members: frozenset[str] = frozenset()
    active_agents: Annotated[int, WireExclude()] = 0
    registered_agents: Annotated[int, WireExclude()] = 0

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
                  if snapshot.status(thread.name).in_view(visibility)}
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
            sum(1 for _ in snapshot.active_threads(registered)), len(registered),
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
    def counts(self, bus: BusReadSource, names: Sequence[str]) -> Mapping[str, InboxCount]: ...


@dataclass(frozen=True)
class CountedRead(InboxRead):
    def counts(self, bus: BusReadSource, names: Sequence[str]) -> Mapping[str, InboxCount]:
        # One bus snapshot for every thread, not one per thread.
        return {name: CountedInbox(count) for name, count in bus.pending_counts_all(names).items()}


@dataclass(frozen=True)
class UncountedRead(InboxRead):
    def counts(self, bus: BusReadSource, names: Sequence[str]) -> Mapping[str, InboxCount]:
        return {name: UncountedInbox() for name in names}


@dataclass(frozen=True, slots=True)
class ThreadRecord(CliResult):
    """A thread's wire record; every reader of one thread writes these fields from here."""

    thread: Annotated[Thread, WireInline()]
    status: Annotated[ThreadStatus, ThreadStatusName]
    activity: Annotated[ActivityData, PresenceExclude()]
    goal_execution: GoalExecution
    pending: Annotated[InboxCount, WireOmitDefault()] = field(
        default=UncountedInbox(), kw_only=True
    )

    @classmethod
    def read(cls, snapshot: RegistrySnapshot, agents: AgentActivity,
             goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers], threads: Iterable[Thread],
             pending: Mapping[str, InboxCount]) -> tuple[ThreadRecord, ...]:
        """Only active threads are observed; the rest show the absent activity."""
        threads = tuple(threads)
        waits = partial(GoalWaits.for_goal, rows=goals.waits.read())
        observed = agents.all_activity(snapshot.active_threads(threads), snapshot=snapshot)
        return tuple(cls(
            thread, snapshot.status(thread.name),
            observed.get(thread.name, Activity.absent(thread.name)),
            goals.snapshot_for(thread, snapshot, waits).execution,
            pending=pending[thread.name],
        ) for thread in threads)

    def to_wire(self) -> object:
        return FieldCodec.project(self, PresenceExclude)

    @projected(view=PresenceExclude)
    def is_fork(self) -> bool:
        return self.thread.is_fork

    @projected(view=PresenceExclude, name="activity")
    def activity_state(self) -> ActivityState:
        return self.activity.state

    @projected(view=PresenceExclude)
    def activity_detail(self) -> str:
        shown = self.activity.presentation(self.thread.display_title)
        return " · ".join(part for part in (self.activity.detail, *shown.status.details) if part)


@dataclass(frozen=True, slots=True)
class ThreadView(ThreadRecord):
    activity: Annotated[ObservedActivity, PresenceExclude()]
    runtime: Annotated[RuntimeInfo, PresenceExclude()]
    last_seen: float
    binding: Annotated[ThreadOwnerBinding, PresenceExclude()] = UnavailableThreadOwnerBinding()

    @classmethod
    def roster(
        cls, snapshot: RegistrySnapshot, agents: AgentActivity,
        goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers],
        visibility: RosterVisibility, *, names: Iterable[str] | None = None,
    ) -> tuple[ThreadView, ...]:
        threads = tuple(snapshot.visible_threads(visibility, names=names))
        if not threads:
            return ()
        runtime = agents.runtime_info.for_threads(thread.name for thread in threads)
        waits = partial(GoalWaits.for_goal, rows=goals.waits.read())
        # A standby goal asks whether its wait targets work; one capture of the rows and
        # their goals' targets answers both, where each target once re-read its own.
        targets = {snapshot.canonical_name(target.incarnation.name)
                   for thread in threads for goal in thread.goal for target in waits(goal).targets}
        captured = {thread.name: thread for thread in threads} | {
            name: snapshot.threads[name] for name in targets if name in snapshot.threads}
        activity = CapturedActivity(agents.all_activity(captured.values(), snapshot=snapshot))
        executions = goals.snapshots_for(threads, snapshot, waits, activity)
        return tuple(cls(
            thread, snapshot.status(thread.name), activity.observed[thread.name],
            goal.execution, runtime[thread.name],
            snapshot.seen_at(thread.name), snapshot.owner_binding(thread.name),
        ) for thread, goal in zip(threads, executions, strict=True))

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        ordinary = self.status.presentation(self.thread.display_title, self.activity,
                                            self._idle_presentation)
        return replace(self.thread.presentation(self.status, ordinary, self.binding),
                       binding=self.binding)

    def _idle_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        if self.thread.executing:
            return ActivityState.WORKING.presentation(ordinary.title, "In a turn")
        return self.goal_execution.waiting_presentation(ordinary)

    @projected(view=PresenceExclude)
    def resumable(self) -> bool:
        return self.thread.runtime.resumable

    @projected(view=PresenceExclude)
    def last_activity(self) -> float:
        return self.activity.timestamp

    @projected(view=PresenceExclude)
    def context_used(self) -> PiCount:
        return self.runtime.context_used

    @projected(view=PresenceExclude)
    def context_size(self) -> PiCount:
        return self.runtime.context_size


@dataclass(frozen=True, slots=True)
class CoordinationSnapshot:
    threads: Mapping[str, ThreadView]
    channels: Mapping[str, ChannelView]
    unread: Mapping[str, UnreadPresentation]
    channel_unread: Mapping[str, UnreadPresentation]
    last_sent: Mapping[str, float]
    references: CurrentDisplayReferences
    channel_order: type[ChannelSort] = NameChannelSort
    thread_unread: Mapping[str, UnreadPresentation] = field(default_factory=dict)
    read_marker_notice: ReadNotice = field(default_factory=NoReadNotice)

    @classmethod
    def capture(
        cls, root: Path, registry: RegistrySnapshot, catalog: CatalogDocument,
        agents: AgentActivity, declarations: Channels,
        sent: Mapping[str, float], messages: Mapping[str, ChannelActivity], *,
        goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers], unread: Mapping[str, int], channel_unread: Mapping[str, int],
        visibility: RosterVisibility, read_marker_notice: ReadNotice = NoReadNotice(),
    ) -> CoordinationSnapshot:
        """Assemble both rosters from the caller's acquired identity and read cut."""
        threads = OrderedDict((view.thread.name, view) for view in ThreadView.roster(
            registry, agents, goals, visibility,
        ))
        activities = {name: view.activity for name, view in threads.items()}
        return cls(
            channels=OrderedDict((view.channel.name, view) for view in ChannelView.roster(
                registry, declarations.visible(visibility), catalog.pinned_members(), catalog.list_order,
                activities, sent, messages, visibility,
            )),
            threads=threads,
            unread={name: ExactUnread(count) for name, count in unread.items()},
            channel_unread={name: ExactUnread(count) for name, count in channel_unread.items()},
            last_sent=sent,
            thread_unread={name: UnobservedUnread() for name in threads},
            references=CurrentDisplayReferences.from_registry(str(root), registry, tuple(declarations)),
            channel_order=catalog.list_order, read_marker_notice=read_marker_notice,
        )

    def has_indexing_unread(self) -> bool:
        """Whether this snapshot still waits for any unread source to finish indexing."""
        return any(
            value.waiting
            for values in (
                self.unread.values(), self.channel_unread.values(), self.thread_unread.values()
            )
            for value in values
        )

    def participants(self, channel: str) -> tuple[ThreadView, ...]:
        view = self.channels.get(channel)
        if view is None:
            return ()
        return tuple(
            member for name in view.members
            if (person := self.threads.get(name)) is not None
            for member in person.status.items_with(ActiveThreadPresence, person)
            if member.presentation.status.working
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


def revision_source(paths: Callable[..., tuple[Path, ...]], *views: type[RevisionView]):
    return WatchPaths(paths), WatchViews(views)


class ReadState(RevisionChanges):
    """Without a revision on either side, every view has changed."""
    __slots__ = ()

    def changed_since(self, view: RevisionView, earlier: ReadState) -> bool:
        return True

    def changed_before(self, view: RevisionView, later: WireRevision) -> bool:
        return True


class NoRevision(ReadState):
    __slots__ = ()


def expiry_period_changed(before: int, now: int) -> bool:
    return WireRevision.period_for(before) != WireRevision.period_for(now)


@dataclass(frozen=True, slots=True)
class WireRevision(ReadState, ToolResult):
    """One observation of every Comms store a view derives from, by name."""

    registry: Annotated[
        tuple[FileRevision, ...],
        *revision_source(
            lambda views: views.registry.store.source_paths(views.root),
            *StatusSourced.views(),
            RegistrationsView,
            ActivityView,
        ),
    ]
    catalog: Annotated[
        tuple[FileRevision, ...],
        *revision_source(
            lambda views: (views.channels.catalog.path,), *StatusSourced.views(), RegistrationsView
        ),
    ]
    bus_changes: Annotated[
        tuple[FileRevision, ...],
        *revision_source(
            lambda views: views.bus.bus.source_paths(views.root), *StatusSourced.views()
        ),
    ]
    bus_identity: Annotated[
        RecordedBusIdentity, WatchViews((*StatusSourced.views(), BusSourceView)),
        Differs(lambda before, current: not before.matches(current)),
    ]
    bus_seq: Annotated[int, WatchViews(StatusSourced.views())]
    history: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: (views.bus.history.path,),
                         *StatusSourced.views(), NotificationsView),
    ]
    activity: Annotated[
        tuple[FileRevision, ...],
        *revision_source(
            lambda views: views.agents.activity.source_paths(views.root),
            *StatusSourced.views(), ActivityView,
        ),
    ]
    runtime_info: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: (views.agents.runtime_info.path,),
                         *StatusSourced.views(), ActivityView),
    ]
    work_room: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: RoomLedger.source_paths(views.registry.storage_rule.room_ledger.root), *StatusSourced.views(), ActivityView),
    ]
    reads: Annotated[
        tuple[FileRevision, ...],
        *revision_source(
            lambda views: (views.bus.reads.path,), *StatusSourced.views(),
            NotificationsView, NotificationEvidenceView,
        ),
    ]
    native_unread: Annotated[
        tuple[FileRevision, ...],
        *revision_source(
            lambda views: views.transcript_reads.source_paths(views.root), StoresView, SidebarView
        ),
    ]
    goal_execution: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: GoalWakeFile.watched_paths_in(views.root), *StatusSourced.views()),
    ]
    relationships: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: (RelationshipFile.path_in(views.root),), *StatusSourced.views()),
    ]
    notifications: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: NotificationAssignment.source_paths(views.root), *StatusSourced.views(), NotificationsView,
                         NotificationEvidenceView),
    ]
    external_transcripts: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: ExternalTranscript.source_paths(views.root), StoresView, ExternalTranscriptsView),
    ]
    team_frames: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: TeamFrameFile.source_paths(views.root), TeamFramesView),
    ]
    # Activity-derived views change once per expiry period, not per tick.
    expiry_tick: Annotated[
        int,
        WatchViews((ActivityView, TeamFramesView)),
        Differs(expiry_period_changed),
    ]

    team_board: Annotated[
        tuple[FileRevision, ...],
        *revision_source(lambda views: TodoStore.source_paths(views.root), TeamBoardView, TeamFramesView),
    ] = ()

    expiry_seconds: ClassVar[int] = 30

    @classmethod
    def empty(cls) -> WireRevision:
        return cls(
            **{
                plan.name: ()
                for plan in FieldCodec.field_plans(cls)
                if WatchPaths.single(plan.annotation)
            },
            bus_identity=NoRecordedBus(), bus_seq=0, expiry_tick=0,
        )

    @classmethod
    def source_paths[Source](cls, views: Source) -> dict[str, tuple[Path, ...]]:
        """Stat and watch the sources declared by the revision fields themselves."""
        return {
            plan.name: source.value(views)
            for plan in FieldCodec.field_plans(cls)
            for source in WatchPaths.single(plan.annotation)
        }

    def selection_reusable_from(self, earlier: WireRevision) -> bool:
        return ((self.registry, self.catalog, self.history) ==
                (earlier.registry, earlier.catalog, earlier.history)
                and not self.changed_since(BusSourceView(), earlier))

    @classmethod
    def period_for(cls, tick: int) -> int:
        return tick // cls.expiry_seconds

    @property
    def expiry_period(self) -> int:
        return self.period_for(self.expiry_tick)

    def changed_since(self, view: RevisionView, earlier: ReadState) -> bool:
        return earlier.changed_before(view, self)

    def changed_before(self, view: RevisionView, later: WireRevision) -> bool:
        """Whether any source feeding `view` differs, by the view's comparison."""
        return any(
            plan.setting(Differs, view.differs)(
                getattr(self, plan.name), getattr(later, plan.name)
            )
            for plan in FieldCodec.field_plans(type(self))
            if isinstance(view, plan.setting(WatchViews, ()))
        )
