"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..comms import Comms
from ..goals import Goal
from ..repo import Repo
from ..standing_rules import (
    DatedStandingRule,
    ReasonedStandingRule,
    UndatedStandingRule,
)
from ..thread_identity import StallWatcherIncarnation
from ..todos import (
    FlatTrend, OrderProgress, PullRequests, WheelSpin,
)
from ..tools import CommsUnjudgedTool
from ..turn_digests import TurnDigestRead, TurnsSince
from .config import CommanderConfig


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
                     for request, served in goal.order.open_requests(
                         goal, comms.goals, thread.incarnation)
                     if not served)

    def line(self) -> str:
        return (f"REQUEST STALLED: {self.goal.id} {self.goal.text}"
                " -> order its next step or mark it done")


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

    def enforce(self, comms: Comms, commander: str) -> None:
        for unit in self.idle(comms, commander)[:1]:
            self.assign(comms, commander, unit, StallWatcherIncarnation.watcher())


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = CommsUnjudgedTool.count(comms, commander)
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
    def merges(cls, repo: Repo) -> Iterator[MergeGrowth]:
        """Read first-parent growth from one captured, already-fetched Git history.

        Fetch belongs to the checkout's update owner, not a standing-rule read.
        Git emits each commit's first-parent diff in this same process.
        """
        log = cls.git(
            repo.checkout,
            "log",
            "--first-parent",
            f"--since={cls.window}",
            "--format=%x1e%H%x00%ct%x00%s",
            "--numstat",
            "--no-renames",
            "--diff-merges=first-parent",
            "origin/main",
            "--",
            repo.source,
            "tests",
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
        return tuple(growth for repo in CommanderConfig.installed().repos
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
