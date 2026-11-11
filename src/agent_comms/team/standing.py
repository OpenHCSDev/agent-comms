"""Standing rules read from the teammate's own files, members of Core's StandingRule family."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import ClassVar, Self

from agent_comms import team
from agent_comms.cli_bridges.claude_protocol import OwedProgress
from agent_comms.cli_bridges.claude_transcript import ClaudeTranscript
from agent_comms.cli_bridges.session_watch import DeafWatch
from agent_comms.cli_bridges.shared import Serve
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import ServicePassStore, TeamPass
from agent_comms.execution.turn_bound import TurnsSince
from agent_comms.execution.turn_digests import TurnDigestRead
from agent_comms.execution.watch_passes import WatchPass
from agent_comms.heartbeat_subjects import (
    HeadSubject,
    HeartbeatSubject,
    PassSubject,
    PhaseSubject,
    RequestSubject,
)
from agent_comms.installer.build_identity import (
    InstalledBuild,
    RecordedBuildIdentity,
    RepositoryCommit,
)
from agent_comms.installer.interpreter_operation import InstallPhase, LauncherPhase
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.logs import InstallLogs, InstallRunning, InstallUnit
from agent_comms.installer.request import InstallRequest
from agent_comms.installer.runtime_resolution import CorePackage
from agent_comms.installer.upgrade import RehearsedUpgrade
from agent_comms.messaging.bus import BusReader
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.messages import Message
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, GoalId, Id128
from agent_comms.planning.goal_actions import ReissuedOrderGoalAction
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.orders import (
    Assignment,
    OwnerRequest,
    ProgressRead,
    PullRequests,
    RestoreHolderTodo,
    Todo,
    UnmergedListedPullRequest,
)
from agent_comms.planning.owner_requests import OwnerRequestId
from agent_comms.planning.plan_phase_base import PlanPhase
from agent_comms.planning.plan_reading import PlanReading
from agent_comms.planning.work_dispatch import ReusedWorkDispatch, WorkDispatch
from agent_comms.projects.head_review import PullRequestHead
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.owner_notices import OrderNotice, TeamNotice
from agent_comms.team.questions import UnansweredTeammateAsks
from agent_comms.team.standing_rules import (
    ClearingVerb,
    CoreActsStandingRule,
    DatedStandingRule,
    FamilyScan,
    HeartbeatFrame,
    IndependentStandingRule,
    OrderHeadReviewCommands,
    OwedAnswer,
    ReasonedStandingRule,
    StandingRule,
    UndatedStandingRule,
)
from agent_comms.team.team_service import TeamService
from agent_comms.ui_model.teammate_state import AwaitingTeam, TeammateState
from agent_comms_hooks import SubagentId



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
class InstallDue(UndatedStandingRule, FamilyScan):
    """A configured main not contained in the installed build can use an install, from the
    pass it leaves the build: due is a disposition, never a launch.

    Every install stops every running unit, so the line names what an install now would
    carry and the open requests about to land, and the teammate batches them into one
    `order install`. That verb launches Core's installer; the log record of its run
    selects the member that renders it.
    """

    repo: RepositoryName
    main: str
    installed: str
    merges: tuple[UninstalledMerge, ...] = field(compare=False)
    landing: tuple[UnmergedListedPullRequest, ...] = field(compare=False)
    logs: InstallLogs = field(compare=False)
    core: Repo = field(compare=False)
    target: str = field(compare=False)
    headline: ClassVar[str] = "INSTALL DUE"
    question: ClassVar[str] = "doing nothing for anyone. why isn't it installed?"

    @property
    @abstractmethod
    def action(self) -> str: ...

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[InstallDue, ...]:
        return cls.due(comms, frame.pulls)

    @staticmethod
    def recorded(comms: Comms, installed: InstalledBuild) -> tuple[tuple[Repo, RepositoryCommit], ...]:
        """The configured repositories the installed build carries and that still have their
        checkout, each with its installed commit: a repository retired between installs
        (refactor-audit after the NRA migration) leaves the build at the next install and never
        needs a second list; a home with nothing installed carries none."""
        return installed.recorded_in(
            repo for repo in comms.registry.snapshot().team_project.require().repos
            if repo.checked_out)

    @classmethod
    def prepare(cls, comms: Comms, holds: HeldOrders,
                pulls: ProgressRead) -> None:
        """Fetching changes Git state, so the heartbeat joins it before observation."""
        for repo, _ in cls.recorded(comms, InstalledBuild.read()):
            repo.git(repo.checkout, "fetch", "-q", repo.remote, "main")

    @classmethod
    def launch_due(cls, comms: Comms) -> tuple[str, ...]:
        """Launch Core's installer for every configured main the installed build lacks: the
        one launch `order install` and the remote install tool both make."""
        pulls = ProgressRead()
        cls.prepare(comms, HeldOrders.read(comms), pulls)
        return tuple(f"{due.repo.to_wire()} main {due.main[:12]}: {due.launch(comms)}"
                     for due in cls.due(comms, pulls))

    @classmethod
    def due(cls, comms: Comms, pulls: PullRequests) -> tuple[InstallDue, ...]:
        DeclaredFamily.discover_package(team)  # The verbs that clear it, for its action.
        repos = comms.registry.snapshot().team_project.require().repos
        logs = InstallLogs(comms.root)
        due: list[InstallDue] = []
        core = {repo.github: repo for repo in repos}[CorePackage.repository]
        target = core.git(core.checkout, "rev-parse", f"{core.remote}/main").strip()
        for repo, recorded in cls.recorded(comms, InstalledBuild.read()):
            main = repo.git(repo.checkout, "rev-parse", f"{repo.remote}/main").strip()
            # Main is in the build exactly when git logs no first-parent commit past it.
            merges = UninstalledMerge.read(repo, recorded.commit, main)
            if not merges:
                continue
            member = InstallDue.member(logs, repo.github, main)
            due.append(member(repo.github, main, recorded.commit, merges,
                              tuple(row for row in pulls.open(repo.github) if row.landing()),
                              logs, core, target))
        return tuple(due)

    @staticmethod
    def member(logs: InstallLogs, repo: RepositoryName, main: str) -> Callable[..., InstallDue]:
        """The merge's launched run selects its standing-rule member; none is pending."""
        return next((RunInstallDue.selected(unit) for unit in logs.launched(repo, main)),
                    PendingInstallDue)

    def launch(self, comms: Comms) -> str:
        """An already-launched merge needs no second attempt."""
        return self.action

    def enforce(self, comms: Comms) -> None:
        """Due is a disposition: the teammate's `order install` launches."""

    def line(self) -> str:
        oldest = RecordedEventTime(min(merge.at for merge in self.merges)).stamp()
        landing = ", ".join(f"#{row.number} {row.branch}" for row in self.landing) or "none"
        return (f"{self.headline} {len(self.merges)} merges sitting on {self.repo.to_wire()} main {self.main[:12]},"
                f" installed is {self.installed[:12]}, oldest merge at {oldest}"
                f" ({'; '.join(merge.line for merge in self.merges)}; landing: {landing})."
                f" {self.question} -> {self.action}")


class PendingInstallDue(InstallDue):
    @property
    def action(self) -> str:
        return f"{self.clearing_commands()} installs Core main {self.target[:12]}; batch the landing requests first"

    def launch(self, comms: Comms) -> str:
        """Every repository's merge installs the same Core target, so a run of that target
        already going is this merge's launch too; any other run is waited out."""
        command = InstallRequest(checkout=self.core.checkout, to=self.target,
                                 project=self.core.checkout)
        try:
            with self.logs.launching():
                run = InstallRun.start(command, comms.root, LauncherPhase())
                run.spawn(wait=False)
        except InstallRunning as running:
            for unit in (unit for unit in running.units if InstallRun.read(unit).target == self.target):
                self.logs.record(unit.logs, self.repo, self.main)
            return str(running)
        self.logs.record(run.unit.logs, self.repo, self.main)
        return f"launched an install of Core main {self.target}"


@dataclass(frozen=True)
class RunInstallDue(InstallDue, declared_name=None):
    """A merge whose launched run selected this member; the run rides along so its
    rendering reads the unit that selected it, not a second look at the logs."""

    unit: InstallUnit = field(compare=False, kw_only=True)

    @staticmethod
    def selected(unit: InstallUnit) -> Callable[..., RunInstallDue]:
        """The run hands itself to the member it selects."""
        return partial(DiedInstallDue if unit.died() else LaunchedInstallDue, unit=unit)


class LaunchedInstallDue(RunInstallDue):
    question = "its install is already launched. is it moving, or did it hang?"

    @property
    def action(self) -> str:
        return "an install for this merge was already launched"


class DiedInstallDue(RunInstallDue):
    """Its unit ended without a result; nothing relaunches it until main moves."""

    headline = "INSTALL DIED"
    question = "and the install died. who's reading why?"

    @property
    def action(self) -> str:
        return f"target {self.target[:12]}; {self.unit.errors}: {self.unit.last_error()}"


@dataclass(frozen=True)
class RequestStalled(IndependentStandingRule, UndatedStandingRule):
    """Tristan's own request with nothing serving it: his lines lead the board and wake the
    teammate every beat until an order serves it, never sit under WATCHING."""

    owner_flagged: ClassVar[bool] = True
    urgent: ClassVar[bool] = True

    request: OwnerRequest

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (RequestSubject(self.request.id),)

    @classmethod
    def violations(cls, comms: Comms) -> tuple[RequestStalled, ...]:
        """Every live teammate's open requests, once each: every peer acts for every request, so
        asking per teammate listed each one once per teammate."""
        store = comms.goals.todos
        teammates = TeamService.teammates(comms.registry.snapshot(), comms.goals)
        return tuple(cls(request)
                     for request in store.open_requests_of(
                         tuple(teammate.acting_identity for teammate in teammates))
                     if not store.serving_orders(request.id))

    def line(self) -> str:
        return (f"ADVANCE request {self.request.id.value} ({self.request.text}) has no order serving it."
                " is anyone on it, or did we forget? -> order its next step or mark it done")


class PlanRule(UndatedStandingRule, FamilyScan, declared_name=None):
    """Rules read from the heartbeat's one look at the plans and the teammate's orders."""

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[Self, ...]:
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

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (PhaseSubject(self.phase),)

    def enforce(self, comms: Comms) -> None:
        starter = TeamService.longest_owed(comms.registry, comms.goals)
        self.phase.task.start(comms, starter, self.phase, self.reading)

    def line(self) -> str:
        return (
            f"START {self.phase.plan.declared_name}/{self.phase.declared_name}"
            f" ({self.phase.plan.document}) is ready and nobody holds it; done when"
            f" {self.reading.describe(self.phase.phase_evidence())}. why is it sitting there?"
            f" -> {self.phase.task.instruction}"
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
        return (
            f"UNSTALL {self.phase.plan.declared_name}/{self.phase.declared_name} held since {RecordedEventTime(self.since).stamp()}"
            f" and still not on main ({self.phase.plan.document}). what's actually holding it?"
            " -> find it and change that this wake: integrate steps that only land together,"
            " take it over, or reassign it"
        )


@dataclass(frozen=True)
class TeammateOwedAnswer(OwedAnswer, declared_name=None):
    """A message one unit is owed; it belongs on that unit's observation."""

    unit: ThreadIncarnation

    def questions_for(self, unit: ThreadIncarnation) -> tuple[Message, ...]:
        return (self.message,) if unit == self.unit else ()

    def named_threads(self) -> tuple[ThreadIncarnation, ...]:
        return tuple(dict.fromkeys((self.unit, *super().named_threads())))


@dataclass(frozen=True)
class UnitAskUnanswered(TeammateOwedAnswer, UndatedStandingRule):
    """Message debt shares its source read with unit observations. An ask is owed by the
    teammate it was addressed to, so that teammate is part of the row: one ask to two
    teammates is two debts, each on its own debtor's tool calls. (A delivery's verdict is
    owed by the whole team, so DeliveryAwaitingJudgement names no debtor.)"""

    debtor: ThreadIncarnation = field(kw_only=True)

    def named_threads(self) -> tuple[ThreadIncarnation, ...]:
        return tuple(dict.fromkeys((*super().named_threads(), self.debtor)))

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], debtor: Thread,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message, owner, debtor=debtor.incarnation)
                     for owner, questions in UnansweredTeammateAsks.for_orders(
                         source, snapshot, debtor.name, held, time.time(),
                     ).items()
                     for message in questions)

    def clearing_action(self) -> str:
        return (f"Reply to {self.cited} or use --answers {self.message.message_id}. "
                'If the answer must wait, say "waiting on X, owner Y" now.')

    def due(self, now: float) -> bool:
        return self.message.timestamp <= now - AwaitingTeam.seconds

    def asked_by(self, reached: frozenset[str]) -> bool:
        return self.unit.name in reached

    def answered_in(self) -> tuple[str, ...]:
        return (self.unit.name,)

    def misaddressed(self, named: tuple[Message, ...], reached: frozenset[str]) -> str:
        """A unit's ask is answered only by a message that reaches that unit."""
        if self.unit.name in reached or not self.answered_by(named):
            return ""
        return (f"{self.message.message_id} was asked by {self.unit.name}; "
                f"answer it in a message to {self.unit.name}")

    def line(self) -> str:
        return (f"ANSWER {self.debtor.name} owes {self.unit.name}: asked {self.cited} at {RecordedEventTime(self.message.timestamp).stamp()}. why is it still waiting?"
                f" -> {self.clearing_action()}")


@dataclass(frozen=True)
class DeliveryAwaitingJudgement(TeammateOwedAnswer, UndatedStandingRule, OrderHeadReviewCommands):
    """A holder's delivery needs a verdict or a replacement order, never a chat reply; it offers
    only the verdicts its claimed order takes, the same as every linked head line."""

    problem: str
    request: PullRequestHead

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], debtor: Thread,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        """The retained claim owns the report and the head owed a verdict, published or not:
        a claim whose answer never reached the bus is still owed its judgement."""
        return tuple(cls(message=claim.message, unit=owner, problem=todo.id,
                         request=claim.request(), todo=todo)
                     for owner, todos in held.items() for todo in todos
                     for claim in todo.order.owed_claims())

    @property
    def cited(self) -> str:
        """The claim by its problem and head; its message may have no bus sequence yet."""
        return f"{self.problem} claim of {self.request.head[:9]}"

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        """A verdict on the delivered head acts on the delivery; a chat reply does not."""
        return (HeadSubject(self.request),)

    def command_values(self) -> Mapping[str, str]:
        return {"unit": self.unit.name, **super().command_values()}

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        return False

    def blocks(self, reached: frozenset[str], action: ClearingVerb,
               answered: tuple[OwedAnswer, ...]) -> bool:
        """Work sent to the holder waits on this judgement; an answer to the holder's own ask
        never does, or the gate would refuse the conversation the answer gate demands."""
        return (self.unit.name in reached
                and not action.resolves_claim(self.unit.name, self.problem)
                and not any(rule.asked_by(frozenset((self.unit.name,))) for rule in answered))

    def clearing_action(self) -> str:
        DeclaredFamily.discover_package(team)  # The verbs that clear it, for its action.
        return self.clearing_commands()

    def line(self) -> str:
        return (f"JUDGE {self.unit.name} delivered {self.problem} ({self.cited})."
                f" you judged it yet? -> {self.clearing_action()}")


@dataclass(frozen=True)
class GoalBlocked(IndependentStandingRule, UndatedStandingRule):
    """A visible thread's own goal is blocked, with or without a teammate order."""

    thread: ThreadIncarnation
    goal: GoalId
    reason: str
    urgent: ClassVar[bool] = True

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        return tuple(cls(thread.incarnation, goal.id, reason)
                     for thread in snapshot.visible_threads(DEFAULT_ROSTER)
                     for goal in thread.goal
                     for reason in goal.state.blocked_reasons())

    def named_threads(self) -> tuple[ThreadIncarnation, ...]:
        return (self.thread,)

    def line(self) -> str:
        return (f"GOAL BLOCKED {self.thread.name}: {self.reason[:200]}"
                " -> unblock it or tell Tristan, this wake")


@dataclass(frozen=True)
class HolderGone(IndependentStandingRule, UndatedStandingRule):
    """An order still held by a unit that was deleted or registered again since the order:
    no registered thread can deliver it, so its order state names what clears it."""

    todo: Todo

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        return cls.in_scan(HeldOrders.read(comms))

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.in_scan(frame)

    @classmethod
    def in_scan(cls, scan: HeldOrders) -> tuple[Self, ...]:
        return tuple(cls(todo) for todo in scan.orphaned)

    def line(self) -> str:
        held = self.todo.held()
        return (f"REHOME {self.todo.id}'s holder {held.owner} (born {held.owner_created}) isn't registered"
                f" anymore. so who has it? -> {self.todo.order.orphan_clearing(self.todo)}")


@dataclass(frozen=True)
class UnownedWheelSpin(IndependentStandingRule, UndatedStandingRule, FamilyScan):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a teammate turn, restarting the row's review window at the handoff."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def read_idle(cls, comms: Comms) -> list[Thread]:
        """A fresh read and an already captured frame share the same idle selection."""
        return cls.idle(comms, HeldOrders.read(comms))

    @classmethod
    def idle(cls, comms: Comms, scan: HeldOrders) -> list[Thread]:
        """Available independent executors, regardless of who issued the request."""
        held = {todo.holder for todo in scan.todos}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((candidate for thread in comms.registry.active_threads().values()
                       if thread.name not in held
                       for candidate in thread.order_candidates()
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
    def violations(cls, comms: Comms) -> tuple[UnownedWheelSpin, ...]:
        return cls.scan_orders(comms, HeldOrders.read(comms), PullRequests())

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[UnownedWheelSpin, ...]:
        return cls.scan_orders(comms, frame, frame.pulls)

    @classmethod
    def scan_orders(cls, comms: Comms,
                    scan: HeldOrders, pulls: PullRequests) -> tuple[UnownedWheelSpin, ...]:
        return cls.paired(comms, comms.goals.todos.board(), scan, pulls)

    @classmethod
    def paired(cls, comms: Comms, todos: Iterable[Todo], scan: HeldOrders,
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
        unowned = tuple(todo for todo in rows if todo.id not in protected)
        # Reading every candidate's recent turns is the expensive part; no unowned row, no read.
        idle = [unit for unit in cls.idle(comms, scan)
                if unit.incarnation not in owners] if unowned else []
        found = []
        for todo in unowned:
            if idle:
                unit = idle.pop(0)
                found.append(ReassignedWheelSpin(todo, ReusedWorkDispatch.for_teammate(
                    ReissuedOrderGoalAction.for_todo(todo), unit)))
        covered = {rule.todo.id for rule in (*retained, *found)}
        return (*retained, *found,
                *(WaitingUnownedWheelSpin(todo) for todo in rows if todo.id not in covered))

    def redecided(self, comms: Comms, current: Todo) -> tuple[StandingRule, ...]:
        return type(self).paired(comms, (current,), HeldOrders.read(comms), PullRequests())

    def line(self) -> str:
        return (f"STAFF {self.todo.id} has no owner and waits for a new teammate. who's taking it?"
                " -> order it to a new teammate")


@dataclass(frozen=True, eq=False)
class ReassignedWheelSpin(CoreActsStandingRule, UnownedWheelSpin):
    dispatch: WorkDispatch

    @property
    def unit(self) -> str:
        return self.dispatch.name

    def enforce(self, comms: Comms) -> None:
        unit = self.dispatch.start(comms)
        OrderNotice().publish(comms.messaging, ThreadTarget(unit.name),
            f"{self.line()}\n{self.dispatch.action.order.instructions(self.todo)}")

    def line(self) -> str:
        return (f"STAFF {self.unit} holds no order while {self.todo.id} sits unowned. why were both idle?"
                f" -> Core gives it to {self.unit}")


@dataclass(frozen=True, eq=False)
class RetainedHolderWheelSpin(CoreActsStandingRule, UnownedWheelSpin):
    previous: Assignment

    def comparison_values(self) -> tuple[object, ...]:
        return (*super().comparison_values(), self.previous.owner_identity)

    def enforce(self, comms: Comms) -> None:
        """The order's creator maintains it, so Core restores its holder as that creator."""
        actor = self.todo.creator_identity
        comms.goals.change_problem(self.todo.id, RestoreHolderTodo(
            self.todo.revision, actor, self.previous,
        ), actor)

    def line(self) -> str:
        return (f"KEEP Core restored {self.previous.owner} as {self.todo.id}'s holder. why would you"
                f" hand the row away? -> keep {self.todo.id} with {self.previous.owner}")


class UnownedOrderWatch(WatchPass[tuple[UnownedWheelSpin, ...]]):
    """Keep dispatch preparation joined; only individually audited readers can be dropped.
    One pairing over every unowned order, so an idle unit is offered once; each rule acts as
    its own order's creator."""

    def observe(self, comms: Comms, prepared: None) -> tuple[UnownedWheelSpin, ...]:
        available = comms.goals.todos.reassignable()
        return UnownedWheelSpin.paired(
            comms, available, HeldOrders.read(comms), PullRequests(available))

    def pass_body(self, comms: Comms, observed: tuple[UnownedWheelSpin, ...]) -> None:
        for rule, error in StandingRule.enforce_all(comms, observed).items():
            logging.info("%s", rule.rejected_line(error))


class WaitingUnownedWheelSpin(UnownedWheelSpin):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""


@dataclass(frozen=True)
class HeartbeatSilent(IndependentStandingRule, DatedStandingRule, FamilyScan, declared_name=None):
    """A missing or stale successful pass, independent of whether a DM needed sending."""

    seconds: ClassVar[float] = TeamPass.completion_ceiling()
    """A pass, the loop's sleep and a pass of slack: the team pass declares how long it takes."""
    since: float
    teammate: RecordedThreadIncarnation

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (PassSubject(self.teammate),)

    @classmethod
    def violations(cls, comms: Comms) -> tuple[HeartbeatSilent, ...]:
        """Each live teammate owed service whose own pass is missing or stale."""
        passes = ServicePassStore.at(comms.root).read()
        now = time.time()
        found = []
        rule: HeartbeatSilent
        for owner in TeamService.running(comms.registry.snapshot()):
            incarnation = owner.incarnation.require_recorded()
            completed = passes.teammate_completed.get(incarnation)
            for request in TeamService.requested_by(owner, comms.goals):
                if completed is None or completed < request.created_at:
                    rule = HeartbeatNotStarted(
                        request.created_at + cls.seconds, incarnation, request.id)
                else:
                    rule = MissedHeartbeat(completed + cls.seconds, incarnation)
                if rule.since < now:
                    found.append(rule)
        return tuple(found)

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class MissedHeartbeat(HeartbeatSilent):
    def line(self) -> str:
        return (f"FIX Tristan has been silent: no completed pass since "
                f"{RecordedEventTime(self.since - self.seconds).clock()}, silent past "
                f"{RecordedEventTime(self.since).clock()}. I have to tell you it's hung? why?"
                " -> read the connector journal for the failing pass and fix its owner")


@dataclass(frozen=True)
class HeartbeatNotStarted(HeartbeatSilent):
    request: OwnerRequestId

    def line(self) -> str:
        return (f"START request {self.request.value} has not heard from Tristan, overdue since "
                f"{RecordedEventTime(self.since).clock()}. why isn't it running?"
                " -> start the connector's Tristan pass and fix its failure")


@dataclass(frozen=True)
class InboxUnavailable(IndependentStandingRule, DatedStandingRule, declared_name=None):
    """A thread nothing can reach: every message sent to it waits undelivered.

    Each member owns how its inbox reports the pause; the window and the date are shared.
    """

    seconds: ClassVar[int] = 300
    since: float = field(compare=False)

    @classmethod
    @abstractmethod
    def paused(cls, comms: Comms) -> tuple[Self, ...]:
        """Each pause this member observes, dated when its window closes."""

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        now = time.time()
        return tuple(rule for rule in cls.paused(comms) if rule.since < now)

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class TeamDeaf(InboxUnavailable, declared_name="team_deaf"):
    """A running teammate's session bridge has no confirmed Monitor; its own registration dates
    the pause. Every running teammate is watched, never one lead; a closed session is stopped,
    not deaf."""

    teammate: RecordedThreadIncarnation
    """Whose deafness this is, by birth: a rename keeps the occurrence (and its age), a thread
    replaced under the same name is a new one. The pause time is not identity (compare=False)."""

    @classmethod
    def paused(cls, comms: Comms) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        return tuple(
            cls(paused + cls.seconds, teammate.incarnation.require_recorded())
            for teammate in TeamService.running(snapshot)
            for paused in snapshot.status(teammate.name).paused_since(
                snapshot.seen_at(teammate.name))
        )

    def line(self) -> str:
        return DeafWatch(f"since {RecordedEventTime(self.since - self.seconds).clock()}").line()

    def enforce(self, comms: Comms) -> None:
        """The teammate's own inbox is the dead path, so #comms hears it instead."""
        TeamNotice().announce(comms.messaging, f"{self.teammate.name}: {self.line()}")


@dataclass(frozen=True)
class UnitDeaf(InboxUnavailable):
    """A thread's own drain recorded that it cannot read its inbox; that record dates the pause.
    Every active thread is read, teammates included: a session bridge without a Monitor is
    TeamDeaf's separate signal."""

    unit: RecordedThreadIncarnation
    """Whose drain is deaf, by birth, as TeamDeaf: a rename keeps the occurrence and its age."""

    @classmethod
    def paused(cls, comms: Comms) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        units = list(comms.registry.active_threads().values())
        activity = comms.agents.all_activity(units, snapshot=snapshot)
        return tuple(cls(paused + cls.seconds,
                         snapshot.require(name).incarnation.require_recorded())
                     for name, observed in activity.items()
                     for paused in observed.readiness.unavailable_since())

    def line(self) -> str:
        return (f"UNDEAFEN {self.unit.name} has read none of its inbox since "
                f"{RecordedEventTime(self.since - self.seconds).clock()}, so every order to it"
                " sits unread. who's it supposed to hear? -> read its drain diagnostic and fix"
                " that owner")


@dataclass(frozen=True)
class ForkStalled(IndependentStandingRule, DatedStandingRule):
    """A Claude fork of a live teammate session wrote nothing to its transcript for the quiet
    window and still owes a next record: its newest deciding record is a tool call or a
    prompt, never an answer. A bus unit's silence shows on its card; a fork has no card, so
    its own transcript, beside its parent session's, is the measure."""

    fork: ThreadIncarnation
    owed_since: float
    parent: str = field(compare=False)
    written: float = field(compare=False)
    progress: OwedProgress = field(compare=False)
    seconds: ClassVar[int] = 300

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        """Each teammate session's registered, unarchived forks. Claude runs a fork inside its
        session's process, so only a session whose bridge process is alive has any."""
        snapshot = comms.registry.snapshot()
        teammates = {thread.name: thread for thread in TeamService.running(snapshot)}
        now = time.time()
        return tuple(
            cls(fork.incarnation, owed.at, record.bus, written, owed)
            for bridge in Serve.running(Path.home()) if bridge.observed.owner.alive()
            for record in bridge.claim.bindings() if record.bus in teammates
            for forks in ({thread.name: thread for thread in snapshot.visible_threads(
                DEFAULT_ROSTER, names=(fork.name for fork in TeamService.forks(
                    snapshot, (teammates[record.bus],))))},)
            for path in ClaudeTranscript.subagents(bridge.observed.transcript)
            for name in (SubagentId.of_transcript(path).fork,) if name in forks
            for fork in (forks[name],)
            for written in (path.stat().st_mtime,) if now - written >= cls.seconds
            for owed in ClaudeTranscript(path).progress().owing()
        )

    def began(self) -> float:
        return self.written + self.seconds

    def line(self) -> str:
        now = time.time()
        return (f"STALLED fork {self.fork.name} of {self.parent} has written nothing for"
                f" {(now - self.written) / 60:.0f} min; {self.progress.doing(now)}. who's driving it?"
                " -> read its tool calls and send its next step (a dead one:"
                f" `agent-comms-team end --fork {self.fork.name}`)")


@dataclass(frozen=True)
class SlowInstallPhase(IndependentStandingRule, UndatedStandingRule):
    """A phase of the install that built the running runtime took longer than the healthy
    line (rules/no-waiting.md). The install's own result record holds every phase's
    exclusive seconds; its stderr and bus summary are renderings of the same clock."""

    install: Id128
    phase: InstallPhase
    seconds: float = field(compare=False)
    healthy: ClassVar[float] = 60

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        """The newest finished run whose target is the installed Core commit: a rehearsal of
        a newer tree writes phases this runtime may not declare, so only its own run is read."""
        installed = frozenset(record.commit for record in RecordedBuildIdentity.installed().commits)
        finished = sorted((unit for unit in InstallLogs(comms.root).units()
                           if InstallRun.read(unit).target in installed
                           and unit.result.stat().st_size),
                          key=lambda unit: unit.result.stat().st_mtime)
        return tuple(
            cls(unit.identity, timed.phase, timed.seconds)
            for unit in finished[-1:]
            for timed in FieldCodec.decode(RehearsedUpgrade, FieldCodec.parse(unit.result.read_text())).phases
            if timed.seconds > cls.healthy
        )

    def line(self) -> str:
        return (f"SLOW install phase {self.phase.label()} took {self.seconds:.0f} s in the install that"
                f" built this runtime ({self.install}), healthy line {self.healthy:.0f} s. whose phase is"
                " eating the install? -> order the fix to that phase's owner")


@dataclass(frozen=True)
class Scrutinize(IndependentStandingRule, UndatedStandingRule):
    threshold: ClassVar[int] = 2
    holder: Assignment
    todo: Todo
    count: int

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        return tuple(cls(held, todo, count)
                     for todo in comms.goals.todos.held_orders()
                     for held in todo.current_holder.current_assignments()
                     for count in (todo.strikes_against(held.owner_identity),)
                     if count >= cls.threshold)

    def line(self) -> str:
        return (f"SCRUTINIZE {self.holder.owner} {self.todo.id}: {self.count} strikes"
                " -> read the teammate's trail and diff, name the cause, and order the fix to the cause")


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = ("UNJUDGED {why}. who's reviewing this pile? -> judge ok --pr N --head SHA on each"
            " reviewed head (one verdict judges every edit before it); fork judges for the rest")

    @classmethod
    def reason(cls, comms: Comms, frame: HeartbeatFrame) -> str | None:
        """The total, then each order's edits made since its last head verdict."""
        counts = frame.edit_counts(comms)
        total = sum(counts.values())
        if total <= cls.limit:
            return None
        named = ", ".join(f"{hold.holder.name} {hold.todo.id} {count}"
                          for hold in frame.live for count in (counts[hold.todo.id],) if count)
        return f"{total} edits since each order's last head verdict: {named}"
