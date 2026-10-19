"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..comms import Comms
from ..standing_rules import (DatedStandingRule, ReasonedStandingRule, StandingRule,
                              UndatedStandingRule)
from ..goals import Goal
from ..threads import Thread
from ..todos import FlatTrend, OrderProgress, PullRequests, StrikeTodo, Todo
from ..turn_digests import TurnDigestRead, TurnsSince
from . import capacity, judge
from .config import CommanderConfig, Repo

CONFIG = CommanderConfig.installed()


@dataclass(frozen=True)
class Detached(UndatedStandingRule):
    unit: str
    unread: int
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
                     for request, served in goal.order.open_requests(goal, comms.goals, thread.incarnation)
                     if not served)

    def line(self) -> str:
        return (f"REQUEST STALLED: {self.goal.id} {self.goal.text}"
                " -> order its next step or mark it done")


@dataclass(frozen=True)
class WheelSpin(StandingRule, declared_name=None):
    """Progress problems read from Core; the commander chooses the next assignee."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str) -> list[Thread]:
        """Units with a native turn in the last ten minutes and no order, parked threads aside."""
        held = {todo.holder for todo in cls.todos(comms, commander)}
        excluded = held | CONFIG.parked | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((thread for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       and TurnDigestRead(comms.root, thread, bound).native()),
                      key=lambda thread: thread.name)


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
                " to done did not fall across two readings -> Core strikes it; the commander"
                " chooses the next unit")

    def enforce(self, comms: Comms, commander: str) -> None:
        todo = comms.goals.todos.get(self.todo.id)
        held = todo.held()
        if not todo.struck_on_order(held.owner):
            comms.goals.change_problem(todo.id, StrikeTodo(
                todo.revision, comms.registry.require(commander).incarnation, held,
                f"{held.owner} kept working on {todo.id} while the distance to done did not fall"
                " across two readings"), commander)


@dataclass(frozen=True)
class UnownedWheelSpin(WheelSpin, UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows."""

    unit: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        rows = (todo for todo in comms.goals.todos.for_commander(
            comms.registry.require(commander).incarnation) if todo.pending and not todo.holder)
        idle = cls.idle(comms, commander)
        return tuple(cls(todo, unit.name) for unit, todo in zip(idle, rows, strict=False))

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.unit} holds no order while {self.todo.id} is unowned"
                f" -> the commander must pick its next owner")


@dataclass(frozen=True)
class ForkNow(UndatedStandingRule):
    """The load on the asked Comms needs more live forks than there are."""

    load: capacity.Load

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[ForkNow, ...]:
        load = capacity.Load.of(comms, commander)
        return (cls(load),) * load.short

    def line(self) -> str:
        return f"FORK NOW: {self.load}"


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = len(judge.Edit.unjudged(comms, commander))
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

    @staticmethod
    def git(checkout: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True
        ).stdout

    @classmethod
    def numstat(cls, checkout: Path, sha: str, path: str) -> tuple[int, int]:
        diff = cls.git(checkout, "diff", "--numstat", f"{sha}^1", sha, "--", path)
        counted = [
            (int(a), int(d))
            for a, d, _ in (row.split("\t", 2) for row in diff.splitlines())
            if a.isdigit()
        ]
        return sum(a for a, _ in counted), sum(d for _, d in counted)

    @classmethod
    def merges(cls, repo: Repo) -> Iterator[MergeGrowth]:
        """Each first-parent commit on the repository's main inside the window, newest first."""
        cls.git(repo.checkout, "fetch", "-q", "origin", "main")
        log = cls.git(
            repo.checkout,
            "log",
            "--first-parent",
            f"--since={cls.window}",
            "--format=%H %ct %s",
            "origin/main",
        )
        for sha, at, subject in (line.split(" ", 2) for line in log.splitlines()):
            src, tests = (cls.numstat(repo.checkout, sha, path) for path in (repo.source, "tests"))
            yield cls(repo.github, sha, subject, float(at), src, tests)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[MergeGrowth, ...]:
        return tuple(growth for repo in CONFIG.repos for growth in cls.merges(repo) if growth.over)

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
