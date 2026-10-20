"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import ClassVar

from ..comms import Comms
from ..goal_actions import OrderGoalAction
from ..goals import Goal
from ..repo import Repo
from ..standing_rules import (
    DatedStandingRule,
    ReasonedStandingRule,
    StandingRule,
    UndatedStandingRule,
)
from ..thread_identity import StallWatcherIncarnation, ThreadIncarnation
from ..threads import Thread
from ..todos import FlatTrend, OrderProgress, PullRequests, Todo
from ..turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from ..work_dispatch import ReusedWorkDispatch, WorkDispatch
from .config import CommanderConfig


@dataclass(frozen=True)
class Detached(UndatedStandingRule):
    unit: str
    unread: int = field(compare=False)
    orders: tuple[str, ...]

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Detached, ...]:
        owed: dict[str, list[str]] = {}
        for todo in cls.todos(comms, commander):
            owed.setdefault(todo.holder, []).append(todo.id)
        statuses = comms.registry.snapshot().statuses
        away = [unit for unit in sorted(owed) if unit not in statuses or not statuses[unit].receiving]
        unread = comms.bus.pending_counts_all(tuple(unit for unit in away if unit in statuses))
        return tuple(cls(unit, unread.get(unit, 0), tuple(owed[unit])) for unit in away)

    def line(self) -> str:
        return (f"DETACHED {self.unit}: {self.unread} unread, orders {', '.join(self.orders)}"
                " -> wake it or reassign")


@dataclass(frozen=True)
class RequestStalled(UndatedStandingRule):
    goal: Goal

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[RequestStalled, ...]:
        thread = comms.registry.require(commander)
        return tuple(cls(request) for goal in thread.goal.active()
                     for request, served in goal.order.open_requests(
                         goal, comms.goals, thread.incarnation)
                     if not served)

    def line(self) -> str:
        return (f"REQUEST STALLED: {self.goal.id} {self.goal.text}"
                " -> order its next step or mark it done")


@dataclass(frozen=True)
class WheelSpin(StandingRule, declared_name=None):
    """An order is not getting closer to done, or a unit sits with no order; Core gives the
    order to a unit that will move it, with no deadline and no commander turn involved."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str) -> list[Thread]:
        """Units with a recent turn in their own transcript and no order, parked threads aside."""
        held = {todo.holder for todo in cls.todos(comms, commander)}
        excluded = held | CommanderConfig.read_root(comms.root).parked | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((thread for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       and TurnDigestRead(comms.root, thread, bound).read().turns),
                      key=lambda thread: thread.name)

    @staticmethod
    def order_for(todo: Todo, commander: str, assigner: ThreadIncarnation) -> OrderGoalAction:
        return OrderGoalAction(
            problem=todo.id, expected_revision=todo.revision,
            order=todo.order.reissued(time.time()), request=todo.goal.request(),
            deadline=todo.deadline, commander=commander, assigner=assigner,
            traced=todo.order.owner)

    @abstractmethod
    def dispatches(self, comms: Comms, commander: str) -> Iterable[WorkDispatch]: ...

    def enforce(self, comms: Comms, commander: str) -> None:
        for dispatch in self.dispatches(comms, commander):
            unit = dispatch.start(comms)
            comms.messaging.send(commander, unit.name,
                f"{self.line()}\n{dispatch.action.order.instructions(self.todo)}")


@dataclass(frozen=True)
class FlatWheelSpin(WheelSpin, DatedStandingRule):
    """Two distance readings with no fall while the unit kept working."""

    since: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[FlatWheelSpin, ...]:
        held = tuple(todo for todo in cls.todos(comms, commander) if todo.pending)
        pulls = PullRequests(held)
        storage = comms.registry.storage_rule
        trends = ((todo, OrderProgress.read(todo, storage, pulls).distance_readings().trend)
                  for todo in held)
        flat = ((todo, trend.since) for todo, trend in trends if isinstance(trend, FlatTrend))
        return tuple(cls(todo, since) for todo, since in flat
                     if TurnDigestRead(comms.root, comms.registry.require(todo.held().owner),
                                       TurnsSince(since)).read().turns)

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.todo.holder} kept working on {self.todo.id} while its distance"
                " to done did not fall across two readings -> Core strikes it and gives the order"
                " to an idle unit")

    def dispatches(self, comms: Comms, commander: str) -> Iterable[WorkDispatch]:
        for unit in self.idle(comms, commander)[:1]:
            yield ReusedWorkDispatch(self.order_for(
                self.todo, commander, StallWatcherIncarnation.watcher()), self.todo.revision, unit)


@dataclass(frozen=True)
class UnownedWheelSpin(WheelSpin, UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows."""

    dispatch: WorkDispatch

    @property
    def unit(self) -> str:
        return self.dispatch.name

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        actor = comms.registry.require(commander).incarnation
        rows = tuple(todo for todo in comms.goals.todos.for_commander(actor)
                     if todo.pending and not todo.holder)
        paired = cls.paired(comms, commander, rows, actor)
        assigned = {rule.todo.id for rule in paired}
        return paired + tuple(cls(todo, WorkDispatch(
            cls.order_for(todo, commander, actor), todo.revision))
            for todo in rows if todo.id not in assigned)

    @classmethod
    def paired(cls, comms: Comms, commander: str, todos: Iterable[Todo],
               assigner: ThreadIncarnation) -> tuple[UnownedWheelSpin, ...]:
        """Each pending row with the first idle unit not struck on it."""
        idle, found = cls.idle(comms, commander), []
        for todo in (todo for todo in todos if todo.pending):
            for unit in idle:
                if not todo.live_strikes(unit.name):
                    idle.remove(unit)
                    found.append(cls(todo, ReusedWorkDispatch(
                        cls.order_for(todo, commander, assigner), todo.revision, unit)))
                    break
        return tuple(found)

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.unit} holds no order while {self.todo.id} is unowned"
                f" -> Core gives it to {self.unit}")

    def dispatches(self, comms: Comms, commander: str) -> Iterable[WorkDispatch]:
        return (self.dispatch,)

    @classmethod
    def after_stall(cls, comms: Comms) -> None:
        """Assign first: refused or unavailable replacements leave the old holder untouched."""
        stalled = comms.goals.todos.stalled(comms.registry.storage_rule)
        for commander in {todo.creator_identity.name for todo in stalled}:
            mine = (todo for todo in stalled if todo.creator_identity.name == commander)
            for rule in cls.paired(comms, commander, mine, StallWatcherIncarnation.watcher()):
                rule.enforce(comms, commander)

    @classmethod
    def watch_pass(cls, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        """The worker borrows the root for this pass, never while the timer is idle."""
        with opened_wire() as comms:
            comms.goals.todos.advance_objective_failures(comms.goals)
            cls.after_stall(comms)


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = OrderEditCountRead.count(comms, commander)
        return str(total) if total > cls.limit else None


@dataclass(frozen=True)
class MergeGrowth(DatedStandingRule):
    """A merge to a fleet repository's main that grew its source by more than the limit, judged by
    its own first-parent diff and read from git on every read; nothing about it is stored."""

    limit: ClassVar[int] = 40
    window: ClassVar[str] = "12 hours ago"
    repo: str
    sha: str
    subject: str
    merged: float
    src: tuple[int, int]
    tests: tuple[int, int]

    @classmethod
    def merges(cls, repo: Repo) -> Iterator[MergeGrowth]:
        """Read first-parent growth from one captured, already-fetched Git history.

        Fetch belongs to the checkout's update owner, not a standing-rule read.
        Git emits each commit's first-parent diff in this same process.
        """
        log = repo.git(
            repo.checkout, "log", "--first-parent", f"--since={cls.window}",
            "--format=%x1e%H%x00%ct%x00%s", "--numstat", "--no-renames",
            "--diff-merges=first-parent", "origin/main", "--", repo.source, "tests",
        )
        for record in log.split("\x1e")[1:]:
            header, _, diff = record.partition("\n")
            sha, at, subject = header.split("\x00", 2)
            src, tests = [0, 0], [0, 0]
            for row in diff.splitlines():
                if not row:
                    continue
                added, deleted, path = row.split("\t", 2)
                if not (added.isdigit() and deleted.isdigit()):
                    continue
                for root, counts in ((repo.source, src), ("tests", tests)):
                    if path == root or path.startswith(root.rstrip("/") + "/"):
                        counts[0] += int(added)
                        counts[1] += int(deleted)
            yield cls(repo.github, sha, subject, float(at), (src[0], src[1]), (tests[0], tests[1]))

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[MergeGrowth, ...]:
        return tuple(growth for repo in CommanderConfig.read_root(comms.root).repos
                     for growth in cls.merges(repo) if growth.over)

    @property
    def over(self) -> bool:
        net, tests = self.src[0] - self.src[1], self.tests[0] - self.tests[1]
        return net > self.limit or tests > max(net, 0)

    def began(self) -> float:
        return self.merged

    def line(self) -> str:
        (a, d), (ta, td) = self.src, self.tests
        return (
            f"GROWTH {self.repo} {self.sha[:9]} src +{a}/-{d} (net {a - d:+}) tests +{ta}/-{td}: "
            f"{self.subject[:90]} -> justify or send back the owner with a deletion order"
        )
