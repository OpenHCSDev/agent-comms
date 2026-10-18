"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..comms import Comms
from ..standing_rules import ReasonedStandingRule, StandingRule
from . import capacity, judge
from .config import CommanderConfig, Repo

CONFIG = CommanderConfig.installed()


@dataclass(frozen=True)
class IdleUnits(ReasonedStandingRule):
    """Units active in the recent log holding no order; parked threads and the commander are not."""

    recent: ClassVar[int] = 6000
    text = "IDLE UNITS with no order: {why} -> give each the top unowned row now"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        with open(CONFIG.state / "activity.log", errors="replace") as log:
            seen = {line[9:].split(" ", 1)[0] for line in log.readlines()[-cls.recent :]}
        held = {todo.holder for todo in cls.todos(comms, commander) if todo.state.open}
        idle = sorted(seen - held - CONFIG.parked - {commander, ""})
        return ", ".join(idle) or None


@dataclass(frozen=True)
class ForkNow(ReasonedStandingRule):
    text = "FORK NOW: {why}"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        return capacity.short()


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = len(judge.unjudged())
        return str(total) if total > cls.limit else None


@dataclass(frozen=True)
class MergeGrowth(StandingRule):
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
