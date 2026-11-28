"""Thread wire records, presence views, and inbox unread-count policies."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Annotated

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.child_process import NoProcess, ProcessState
from agent_comms.execution.runtime_info import RuntimeInfo
from agent_comms.messaging.bus_read_source import BusReadSource
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    JsonShapeFamily,
    JsonShapeMember,
    PresenceExclude,
    WireInline,
    WireOmitDefault,
    projected,
)
from agent_comms.pi_counts import PiCount
from agent_comms.planning.goal_execution import GoalExecution
from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot
from agent_comms.planning.goal_snapshots import GoalSnapshots
from agent_comms.planning.goal_waits import GoalWaits
from agent_comms.registry.activity import Activity, ActivityData, ActivityState, ObservedActivity
from agent_comms.registry.agent_activity import ActivityAnswers, AgentActivity, CapturedActivity
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER, RosterVisibility
from agent_comms.registry.thread_presentation import (
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from agent_comms.registry.thread_status import ActiveThreadPresence, ThreadStatus, ThreadStatusName
from agent_comms.registry.threads import Thread


@dataclass(frozen=True)
class InboxCount(DeclaredFamily, JsonShapeFamily, affix="Inbox"):
    """A thread's unread count as its reader took it: counted, or not counted at all."""

    def unread_count(self) -> int:
        return 0


@dataclass(frozen=True)
class CountedInbox(InboxCount, JsonShapeMember):
    value: int

    def unread_count(self) -> int:
        return self.value


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
    native_process_identity: Annotated[ProcessState, WireOmitDefault()] = field(
        default_factory=NoProcess, kw_only=True
    )

    @classmethod
    def list_threads(
        cls,
        snapshot: RegistrySnapshot,
        bus: BusReadSource,
        agents: AgentActivity,
        goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers],
        *,
        active_only: bool = False,
    ) -> tuple[ThreadRecord, ...]:
        """Summarize the default roster's threads with status and pending counts. Archived
        threads have their own readers; listing them here counted thousands of retired inboxes
        on every call."""
        threads = tuple(sorted(snapshot.roster(DEFAULT_ROSTER), key=lambda thread: thread.name))
        if active_only:
            threads = tuple(snapshot.active_threads(threads))
        pending = CountedRead().counts(bus, tuple(thread.name for thread in threads))
        return cls.read(snapshot, agents, goals, threads, pending)

    @classmethod
    def read(
        cls,
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
        goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers],
        threads: Iterable[Thread],
        pending: Mapping[str, InboxCount],
    ) -> tuple[ThreadRecord, ...]:
        """Only active threads are observed; the rest show the absent activity."""
        threads = tuple(threads)
        statuses = {thread.name: snapshot.status(thread.name) for thread in threads}
        active = tuple(
            member
            for thread in threads
            for member in statuses[thread.name].items_with(ActiveThreadPresence, thread)
        )
        waits = partial(GoalWaits.for_goal, rows=goals.waits.read())
        observed = agents.all_activity(active, snapshot=snapshot)
        executions = goals.snapshots_for(threads, snapshot, waits, agents)
        return tuple(
            cls(
                thread,
                statuses[thread.name],
                observed.get(thread.name, Activity.absent(thread.name)),
                goal.execution,
                pending=pending[thread.name],
            )
            for thread, goal in zip(threads, executions, strict=True)
        )

    def to_wire(self) -> object:
        return FieldCodec.project(self, PresenceExclude)

    def compact_text(self) -> str:
        bits = [f"{self.thread.name} [{self.status.declared_name}]"]
        if self.thread.tags:
            bits.append(",".join(f"#{tag}" for tag in sorted(self.thread.tags)))
        unread = self.pending.unread_count()
        if unread:
            bits.append(f"unread:{unread}")
        if self.activity_detail:
            bits.append(f"— {self.activity_detail}")
        return " ".join(bits)

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
    def who(
        cls,
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
        goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers],
    ) -> tuple[ThreadView, ...]:
        """Who is here: live presences, with no unread scan that grows with history."""
        live = tuple(thread.name for thread in snapshot.active_threads(snapshot.active_candidates()))
        return tuple(
            sorted(
                cls.roster(snapshot, agents, goals, DEFAULT_ROSTER, names=live),
                key=lambda view: view.thread.name,
            )
        )

    @classmethod
    def roster(
        cls,
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
        goals: GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot, ActivityAnswers],
        visibility: RosterVisibility,
        *,
        names: Iterable[str] | None = None,
    ) -> tuple[ThreadView, ...]:
        threads = tuple(snapshot.visible_threads(visibility, names=names))
        if not threads:
            return ()
        runtime = agents.runtime_info.for_threads(thread.name for thread in threads)
        waits = partial(GoalWaits.for_goal, rows=goals.waits.read())
        # A standby goal asks whether its wait targets work; one capture of the rows and
        # their goals' targets answers both, where each target once re-read its own.
        targets = {
            snapshot.canonical_name(target.incarnation.name)
            for thread in threads
            for goal in thread.goal
            for target in waits(goal).targets
        }
        captured = {thread.name: thread for thread in threads} | {
            name: snapshot.threads[name] for name in targets if name in snapshot.threads
        }
        activity = CapturedActivity(agents.all_activity(captured.values(), snapshot=snapshot))
        executions = goals.snapshots_for(threads, snapshot, waits, activity)
        return tuple(
            cls(
                thread,
                snapshot.status(thread.name),
                activity.observed[thread.name],
                goal.execution,
                runtime[thread.name],
                snapshot.seen_at(thread.name),
                snapshot.owner_binding(thread.name),
            )
            for thread, goal in zip(threads, executions, strict=True)
        )

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        ordinary = self.status.presentation(
            self.thread.display_title, self.activity, self._idle_presentation
        )
        return replace(
            self.thread.presentation(self.status, ordinary, self.binding), binding=self.binding
        )

    def _idle_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        if self.thread.executing:
            return ActivityState.WORKING.presentation(ordinary.title, "In a turn")
        return self.goal_execution.waiting_presentation(ordinary)

    @projected(view=PresenceExclude)
    def resumable(self) -> bool:
        return self.thread.runtime.is_resumable()

    @projected(view=PresenceExclude)
    def last_activity(self) -> float:
        return self.activity.timestamp

    @projected(view=PresenceExclude)
    def context_used(self) -> PiCount:
        return self.runtime.context_used

    @projected(view=PresenceExclude)
    def context_size(self) -> PiCount:
        return self.runtime.context_size
