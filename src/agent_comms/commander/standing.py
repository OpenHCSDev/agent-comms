"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.bus import BusReader
from agent_comms.channel_targets import CommsTag
from agent_comms import commander
from agent_comms.cli_commands import InstallCliCommand
from agent_comms.commander.questions import UnansweredUnitAsks
from agent_comms.commander_service import CommanderService
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import ReissuedOrderGoalAction
from agent_comms.head_review import PullRequestHead
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.heartbeat_subjects import HeadSubject, HeartbeatSubject
from agent_comms.held_orders import HeldOrders
from agent_comms.installer import launch as installer_launch
from agent_comms.installer.interpreter_operation import LauncherPhase
from agent_comms.installer.runtime_resolution import CorePackage
from agent_comms.message_bus import WatchPass
from agent_comms.message_page import MessageReadRequest
from agent_comms.messages import Message
from agent_comms.owner_requests import OwnerRequestId
from agent_comms.phase_evidence import PlanReading
from agent_comms.plan_phases import PlanPhase
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import (
    DatedStandingRule,
    IndependentStandingRule,
    OwedAnswer,
    ReasonedStandingRule,
    StandingRule,
    UndatedStandingRule,
)
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import (
    Assignment,
    OwnerRequest,
    PullRequests,
    UnmergedListedPullRequest,
    RestoreHolderTodo,
    Todo,
)
from agent_comms.turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from agent_comms.ui_model.unit_state import AwaitingCommander, UnitState
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch
from agent_comms_hooks.watch import DeafWatch

if TYPE_CHECKING:
    from agent_comms.commander.order import CommanderVerb


@dataclass(frozen=True)
class UninstalledMerge:
    """One first-parent commit on main the installed build lacks, decoded from git's log once."""

    at: int
    request: str
    title: str
    format: ClassVar[str] = "%ct%x1f%s%x1f%b%x1e"

    @classmethod
    def read(cls, repo: Repo, installed: str, main: str) -> tuple[Self, ...]:
        """GitHub's merge subject names the request; its body's first line is the title."""
        log = repo.git(repo.checkout, "log", "--first-parent", f"--format={cls.format}",
                       f"{installed}..{main}")
        return tuple(cls(int(at), subject.removeprefix("Merge pull request ").partition(" from ")[0],
                         body.strip().partition("\n")[0])
                     for record in log.split("\x1e")[:-1]
                     for at, subject, body in (record.lstrip("\n").split("\x1f"),))

    @property
    def line(self) -> str:
        return f"{self.request} {self.title}"


@dataclass(frozen=True)
class InstallDue(UndatedStandingRule):
    """A configured main not contained in the installed build can use an install, from the
    pass it leaves the build: due is a disposition, never a launch.

    Every install stops every running unit, so the line names what an install now would
    carry and the open requests about to land, and the commander batches them into one
    `order install`. That verb launches Core's installer; the log record of its run
    selects the member that renders it.
    """

    repo: RepositoryName
    main: str
    installed: str
    merges: tuple[UninstalledMerge, ...] = field(compare=False)
    landing: tuple[UnmergedListedPullRequest, ...] = field(compare=False)
    logs: installer_launch.InstallLogs = field(compare=False)
    core: Repo = field(compare=False)
    target: str = field(compare=False)
    headline: ClassVar[str] = "INSTALL DUE"

    @property
    @abstractmethod
    def action(self) -> str: ...

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return InstallDue

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[InstallDue, ...]:
        return cls.due(comms, frame.pulls)

    @classmethod
    def due(cls, comms: Comms, pulls: PullRequests) -> tuple[InstallDue, ...]:
        DeclaredFamily.discover_package(commander)  # The verbs that clear it, for its action.
        repos = comms.registry.snapshot().commander_project.require().repos
        installed = RecordedBuildIdentity.installed()
        logs = installer_launch.InstallLogs(comms.root)
        due: list[InstallDue] = []
        for repo in repos:
            repo.git(repo.checkout, "fetch", "-q", "origin", "main")
        core = {repo.github: repo for repo in repos}[CorePackage.repository]
        target = core.git(core.checkout, "rev-parse", "origin/main").strip()
        for repo in repos:
            main = repo.git(repo.checkout, "rev-parse", "origin/main").strip()
            recorded = installed.for_repository(repo)
            # Main is in the build exactly when git logs no first-parent commit past it.
            merges = UninstalledMerge.read(repo, recorded.commit, main)
            if not merges:
                continue
            member = logs.member(repo.github, main)
            due.append(member(repo.github, main, recorded.commit, merges,
                              tuple(row for row in pulls.open(repo.github) if row.landing()),
                              logs, core, target))
        return tuple(due)

    def launch(self, comms: Comms) -> str:
        """An already-launched merge needs no second attempt."""
        return self.action

    def enforce(self, comms: Comms, commander: str) -> None:
        """Due is a disposition: the commander's `order install` launches."""

    def line(self) -> str:
        oldest = int((time.time() - min(merge.at for merge in self.merges)) // 60)
        landing = ", ".join(f"#{row.number} {row.branch}" for row in self.landing) or "none"
        return (f"{self.headline} ({self.repo.to_wire()}: main {self.main[:12]}, installed "
                f"{self.installed[:12]}, {len(self.merges)} merges, oldest {oldest} min: "
                f"{'; '.join(merge.line for merge in self.merges)}; landing: {landing}) "
                f"-> {self.action}")


class PendingInstallDue(InstallDue):
    @property
    def action(self) -> str:
        verbs = " or ".join(f"`{verb.usage()}`" for verb in self.clearing_verbs)
        return f"{verbs} installs Core main {self.target[:12]}; batch the landing requests first"

    def launch(self, comms: Comms) -> str:
        """Every repository's merge installs the same Core target, so a run of that target
        already going is this merge's launch too; any other run is waited out."""
        command = InstallCliCommand(checkout=self.core.checkout, to=self.target,
                                    project=self.core.checkout)
        try:
            with self.logs.launching():
                run = installer_launch.InstallRun.start(command, comms.root, LauncherPhase())
                run.spawn(wait=False)
        except installer_launch.InstallRunning as running:
            for unit in running.installing(self.target):
                self.logs.record(unit.logs, self.repo, self.main)
            return str(running)
        self.logs.record(run.unit.logs, self.repo, self.main)
        return f"launched an install of Core main {self.target}"


@dataclass(frozen=True)
class RunInstallDue(InstallDue, declared_name=None):
    """A merge whose launched run selected this member; the run rides along so its
    rendering reads the unit that selected it, not a second look at the logs."""

    unit: installer_launch.InstallUnit = field(compare=False, kw_only=True)


class LaunchedInstallDue(RunInstallDue):
    @property
    def action(self) -> str:
        return "an install for this merge was already launched"


class DiedInstallDue(RunInstallDue):
    """Its unit ended without a result; nothing relaunches it until main moves."""

    headline = "INSTALL DIED"

    @property
    def action(self) -> str:
        return f"target {self.target[:12]}; {self.unit.errors}: {self.unit.last_error()}"


@dataclass(frozen=True)
class RequestStalled(IndependentStandingRule, UndatedStandingRule):
    request: OwnerRequest

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[RequestStalled, ...]:
        thread = comms.registry.require(commander)
        store = comms.goals.todos
        return tuple(cls(request) for request in store.open_requests(thread.incarnation)
                     if not store.serving_orders(request.id))

    def line(self) -> str:
        return (f"ADVANCE request {self.request.id.value} {self.request.text}"
                " -> order its next step or mark it done")


@dataclass(frozen=True)
class AnswersTo(MessageReadRequest[tuple[Message, ...]]):
    """The newest reply to an original from the requested participants."""

    scope: RepliesDisplayScope
    since: float
    empty: ClassVar[tuple[Message, ...]] = ()

    def read_opened(self, source: BusReader) -> tuple[Message, ...]:
        return source.latest_answer(
            self.scope, since=self.since,
        )


class PlanRule(UndatedStandingRule, declared_name=None):
    """Rules read from the heartbeat's one look at the plans and the commander's orders."""

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return PlanRule

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return tuple(rule for member in cls.members_with(cls) for rule in member.found(frame.plan))

    @classmethod
    @abstractmethod
    def found(cls, reading: PlanReading) -> tuple[Self, ...]: ...


@dataclass(frozen=True)
class PlanIdle(PlanRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]
    reading: PlanReading = field(compare=False)

    @classmethod
    def found(cls, reading: PlanReading) -> tuple[Self, ...]:
        return tuple(
            cls(phase, reading)
            for phase in PlanPhase.members_with(PlanPhase)
            if phase.ready(reading) and not phase.ordered(reading)
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        self.phase.task.start(comms, comms.registry.require(commander), self.phase, self.reading)

    def line(self) -> str:
        return (
            f"START {self.phase.plan.declared_name}/{self.phase.declared_name}"
            f" ({self.phase.plan.document}): ready, no open order; done when"
            f" {self.reading.describe(self.phase.evidence)} -> {self.phase.task.instruction}"
        )


@dataclass(frozen=True)
class PlanStalled(PlanRule):
    """A ready phase that a unit has held past the window while main still lacks it. Holding an
    order is not moving the plan: something structural keeps it still (steps that only land
    together, a blocked holder, a missing owner), and re-sending the order changes nothing."""

    phase: type[PlanPhase]
    since: float
    window: ClassVar[int] = 2 * 60 * 60

    @classmethod
    def found(cls, reading: PlanReading) -> tuple[Self, ...]:
        now = time.time()
        return tuple(
            cls(phase, since)
            for phase in PlanPhase.members_with(PlanPhase)
            if phase.ready(reading) and phase.ordered(reading)
            for since in (phase.ordered_since(reading),)
            if now - since >= cls.window
        )

    def line(self) -> str:
        hours = (time.time() - self.since) / 3600
        return (
            f"UNSTALL {self.phase.plan.declared_name}/{self.phase.declared_name}:"
            f" held {hours:.1f}h and not on main ({self.phase.plan.document}) -> find what holds"
            " it still and change that this wake: integrate steps that only land together,"
            " take it over, or reassign it"
        )


@dataclass(frozen=True)
class UnitOwedAnswer(OwedAnswer, declared_name=None):
    """A message one unit is owed; it belongs on that unit's observation."""

    unit: str

    def questions_for(self, unit: ThreadIncarnation) -> tuple[Message, ...]:
        return (self.message,) if unit.name == self.unit else ()


@dataclass(frozen=True)
class UnitAskUnanswered(UnitOwedAnswer, UndatedStandingRule):
    """Message debt shares its source read with unit observations."""

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message, owner.name)
                     for owner, questions in UnansweredUnitAsks.for_orders(
                         source, snapshot, commander, held, time.time(),
                     ).items()
                     for message in questions)

    def clearing_action(self) -> str:
        return (f"Reply to {self.cited} or use --answers {self.message.message_id}. "
                'If the answer must wait, say "waiting on X, owner Y" now.')

    def due(self, now: float) -> bool:
        return self.message.timestamp <= now - AwaitingCommander.seconds

    def asked_by(self, reached: frozenset[str]) -> bool:
        return self.unit in reached

    def answered_in(self) -> tuple[str, ...]:
        return (self.unit,)

    def misaddressed(self, named: tuple[Message, ...], reached: frozenset[str]) -> str:
        """A unit's ask is answered only by a message that reaches that unit."""
        if self.unit in reached or not self.answered_by(named):
            return ""
        return (f"{self.message.message_id} was asked by {self.unit}; "
                f"answer it in a message to {self.unit}")

    def line(self) -> str:
        age = max(0, int((time.time() - self.message.timestamp) // 60))
        return f"ANSWER {self.unit} message {self.cited}: {age} min old"


@dataclass(frozen=True)
class DeliveryAwaitingJudgement(UnitOwedAnswer, UndatedStandingRule):
    """A holder's delivery needs a verdict or a replacement order, never a chat reply."""

    problem: str
    request: PullRequestHead

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(claim.original(source), owner.name, todo.id, claim.request())
                     for owner, todos in held.items() for todo in todos
                     for claim in todo.order.owed_claims())

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        """A verdict on the delivered head acts on the delivery; a chat reply does not."""
        return (HeadSubject(self.request),)

    def clearing_action(self) -> str:
        """The verbs that declare they clear this debt render their own command lines."""
        DeclaredFamily.discover_package(commander)
        lines = " or ".join(f"`{verb.usage()}`" for verb in self.clearing_verbs)
        return (f"{lines} (UNIT {self.unit}, PROBLEM {self.problem})."
                f" --repo: {Repo.selection_refusal()}")

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        return False

    def blocks(self, reached: frozenset[str], action: CommanderVerb,
               answered: tuple[OwedAnswer, ...]) -> bool:
        """Work sent to the holder waits on this judgement; an answer to the holder's own ask
        never does, or the gate would refuse the conversation the answer gate demands."""
        return (self.unit in reached and not action.resolves_claim(self.unit, self.problem)
                and not any(rule.asked_by(frozenset((self.unit,))) for rule in answered))

    def line(self) -> str:
        return (f"JUDGE {self.unit} {self.problem} delivery {self.cited}"
                f" -> {self.clearing_action()}")


@dataclass(frozen=True)
class HolderGone(IndependentStandingRule, UndatedStandingRule):
    """An order still held by a unit that was deleted or registered again since the order:
    no registered thread can deliver it, so its order state names what clears it."""

    todo: Todo

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return cls.in_scan(HeldOrders.read(comms, commander))

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.in_scan(frame)

    @classmethod
    def in_scan(cls, scan: HeldOrders) -> tuple[Self, ...]:
        return tuple(cls(todo) for todo in scan.orphaned)

    def line(self) -> str:
        held = self.todo.held()
        return (f"REHOME {self.todo.id}: holder {held.owner} born {held.owner_created} is no"
                f" longer registered -> {self.todo.order.orphan_clearing(self.todo)}")


@dataclass(frozen=True)
class UnownedWheelSpin(IndependentStandingRule, UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a commander turn, restarting the row's review window at the handoff."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str, scan: HeldOrders) -> list[Thread]:
        """Units whose registered status is running, parked threads aside."""
        held = {todo.holder for todo in scan.todos}
        excluded = held | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((candidate for thread in comms.registry.active_threads().values()
                       if thread.name not in excluded
                       for candidate in thread.execution.order_candidates(thread)
                       if TurnDigestRead(comms.root, candidate, bound).read().turns),
                      key=lambda thread: thread.name)

    def comparison_values(self) -> tuple[object, ...]:
        # Observation times and proposed idle units change; the row and its disposition do not.
        return (self.todo.id,)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UnownedWheelSpin):
            return NotImplemented
        return type(self) is type(other) and self.comparison_values() == other.comparison_values()

    def __hash__(self) -> int:
        return hash((type(self), self.comparison_values()))

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        return cls.scan_orders(comms, commander, HeldOrders.read(comms, commander), PullRequests())

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_orders(comms, commander, frame, frame.pulls)

    @classmethod
    def scan_orders(cls, comms: Comms, commander: str,
                    scan: HeldOrders, pulls: PullRequests) -> tuple[UnownedWheelSpin, ...]:
        actor = comms.registry.require(commander).incarnation
        return cls.paired(comms, commander, comms.goals.todos.for_commander(actor), scan, pulls)

    @classmethod
    def paired(cls, comms: Comms, commander: str,
               todos: Iterable[Todo], scan: HeldOrders,
               pulls: PullRequests) -> tuple[UnownedWheelSpin, ...]:
        """Restore recorded live holders before proposing any cross-unit dispatch."""
        rows = tuple(todo for todo in todos
                     if todo.pending and not todo.holder_state.current_assignments())
        snapshot = comms.registry.snapshot()
        retained = tuple(RetainedHolderWheelSpin(todo, assignment)
                         for todo in rows
                         for assignment in todo.holder_state.retained_assignments(todo, pulls)
                         if assignment.running_holders(snapshot))
        protected = {rule.todo.id for rule in retained}
        owners = {rule.previous.owner_identity for rule in retained}
        idle = [unit for unit in cls.idle(comms, commander, scan) if unit.incarnation not in owners]
        found = []
        for todo in (todo for todo in rows if todo.id not in protected):
            for unit in idle:
                if not todo.strikes_against(unit.incarnation):
                    idle.remove(unit)
                    found.append(ReassignedWheelSpin(todo, ReusedWorkDispatch.for_unit(
                        ReissuedOrderGoalAction.for_todo(todo, commander), unit)))
                    break
        covered = {rule.todo.id for rule in (*retained, *found)}
        return (*retained, *found,
                *(WaitingUnownedWheelSpin(todo) for todo in rows if todo.id not in covered))

    def redecided(self, comms: Comms, commander: str, current: Todo) -> tuple[StandingRule, ...]:
        return type(self).paired(comms, commander, (current,), HeldOrders.read(comms, commander), PullRequests())

    def line(self) -> str:
        return f"STAFF {self.todo.id}: unowned, waits for a new unit"


@dataclass(frozen=True, eq=False)
class ReassignedWheelSpin(UnownedWheelSpin, declared_name=None):
    dispatch: WorkDispatch

    @property
    def unit(self) -> str:
        return self.dispatch.name

    def enforce(self, comms: Comms, commander: str) -> None:
        unit = self.dispatch.start(comms)
        comms.messaging.send(commander, unit.name,
            f"{self.line()}\n{self.dispatch.action.order.instructions(self.todo)}")

    def line(self) -> str:
        return (f"STAFF {self.todo.id} with {self.unit}: it holds no order while the row is unowned"
                f" -> Core gives it to {self.unit}")


@dataclass(frozen=True, eq=False)
class RetainedHolderWheelSpin(UnownedWheelSpin, declared_name=None):
    previous: Assignment

    def comparison_values(self) -> tuple[object, ...]:
        return (*super().comparison_values(), self.previous.owner_identity)

    def enforce(self, comms: Comms, commander: str) -> None:
        comms.goals.change_problem(self.todo.id, RestoreHolderTodo(
            self.todo.revision, comms.registry.require(commander).incarnation, self.previous,
        ), commander)

    def line(self) -> str:
        return (f"KEEP {self.todo.id} with {self.previous.owner}: Core restored its holder;"
                " do not hand the row away")


@dataclass(frozen=True)
class UnownedObservation:
    commander: Thread
    rule: UnownedWheelSpin


class UnownedOrderWatch(WatchPass[tuple[UnownedObservation, ...]]):
    """Observe released work and its checkouts before entering the bus to assign it."""

    def observe(self, comms: Comms) -> tuple[UnownedObservation, ...]:
        available = comms.goals.todos.reassignable()
        observed: list[UnownedObservation] = []
        pulls = PullRequests(available)
        for name in {todo.creator_identity.name for todo in available}:
            commander = comms.registry.require(name)
            mine = (todo for todo in available if todo.creator_identity.name == name)
            observed.extend(
                UnownedObservation(commander, rule)
                for rule in UnownedWheelSpin.paired(
                    comms, name, mine, HeldOrders.read(comms, name), pulls,
                )
            )
        return tuple(observed)

    def pass_body(self, comms: Comms, observed: tuple[UnownedObservation, ...]) -> None:
        for item in observed:
            rejected = StandingRule.enforce_all(comms, item.commander.name, (item.rule,))
            for rule, error in rejected.items():
                logging.info("%s", rule.rejected_line(error))


class WaitingUnownedWheelSpin(UnownedWheelSpin, declared_name=None):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""


@dataclass(frozen=True)
class HeartbeatSilent(IndependentStandingRule, DatedStandingRule, declared_name=None):
    """A missing or stale successful pass, independent of whether a DM needed sending."""

    seconds: ClassVar[int] = 180
    since: float

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return HeartbeatSilent

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[HeartbeatSilent, ...]:
        owner = comms.registry.require(commander)
        completed = ServicePassStore.at(comms.root).read().commander_completed.get(owner.incarnation)
        now = time.time()
        found = []
        for request in CommanderService.requested_by(owner, comms.goals):
            if completed is None or completed < request.created_at:
                rule = HeartbeatNotStarted(request.created_at + cls.seconds, request.id)
            else:
                rule = MissedHeartbeat(completed + cls.seconds)
            if rule.since < now:
                found.append(rule)
        return tuple(found)

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class MissedHeartbeat(HeartbeatSilent):
    def line(self) -> str:
        return (f"FIX THE HEARTBEAT: no completed pass since "
                f"{RecordedEventTime(self.since - self.seconds).clock()}, silent past "
                f"{RecordedEventTime(self.since).clock()}"
                " -> read the connector journal for the failing pass and fix its owner")


@dataclass(frozen=True)
class HeartbeatNotStarted(HeartbeatSilent):
    request: OwnerRequestId

    def line(self) -> str:
        return (f"START THE HEARTBEAT: no beat for request {self.request.value}, overdue since "
                f"{RecordedEventTime(self.since).clock()}"
                " -> start the connector heartbeat and fix its failing pass")


@dataclass(frozen=True)
class InboxUnavailable(IndependentStandingRule, DatedStandingRule, declared_name=None):
    """A thread nothing can reach: every message sent to it waits unread.

    Each member owns how its inbox reports the pause; the window and the date are shared.
    """

    seconds: ClassVar[int] = 300
    since: float = field(compare=False)

    @classmethod
    @abstractmethod
    def paused(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        """Each pause this member observes, dated when its window closes."""

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        now = time.time()
        return tuple(rule for rule in cls.paused(comms, commander) if rule.since < now)

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class CommanderDeaf(InboxUnavailable):
    """The session bridge has no confirmed Monitor; its own registration dates the pause."""

    @classmethod
    def paused(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        pauses = snapshot.status(commander).paused_since(snapshot.seen_at(commander))
        return tuple(cls(paused + cls.seconds) for paused in pauses)

    def line(self) -> str:
        return DeafWatch(f"since {RecordedEventTime(self.since - self.seconds).clock()}").line()

    def enforce(self, comms: Comms, commander: str) -> None:
        """The commander's own inbox is the dead path, so #comms hears it instead."""
        comms.messaging.send(
            CommanderService.sender, CommsTag.channel(), f"{commander}: {self.line()}")


@dataclass(frozen=True)
class UnitDeaf(InboxUnavailable):
    """A unit's own drain recorded that it cannot read its inbox; that record dates the pause."""

    unit: str

    @classmethod
    def paused(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        units = [thread for name, thread in comms.registry.active_threads().items()
                 if name != commander]
        activity = comms.agents.all_activity(units, snapshot=snapshot)
        return tuple(cls(paused + cls.seconds, name)
                     for name, observed in activity.items()
                     for paused in observed.readiness.unavailable_since())

    def line(self) -> str:
        return (f"UNDEAFEN {self.unit}: it has read none of its inbox since "
                f"{RecordedEventTime(self.since - self.seconds).clock()}, so every order to it"
                " waits unread -> read its drain diagnostic and fix that owner")


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = ("UNJUDGED {why} -> accept each reviewed PR head with judge ok --pr N --head SHA"
            " (one verdict judges every edit before it), or fork judges for the rest")

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        """The total, then each order's edits made since its last head verdict."""
        counts = tuple((read, read.read())
                       for read in OrderEditCountRead.for_commander(comms, commander))
        total = sum(count for _, count in counts)
        if total <= cls.limit:
            return None
        named = ", ".join(f"{read.thread.name} {read.todo.id} {count}"
                          for read, count in counts if count)
        return f"{total} edits since each order's last head verdict: {named}"
