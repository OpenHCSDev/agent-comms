"""What proves a plan phase done: facts on its repository's fetched ``origin/main``.

Task acceptance never proves a phase. Each phase declares the evidence its plan's
"Done means" names, and ``PlanPhase.done`` reads only that evidence.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.commander_parity import ParityRow
from agent_comms.repo import Repo

if TYPE_CHECKING:
    from agent_comms.todos import Todo


@dataclass(frozen=True)
class MainTree:
    """One commit of a repository's fetched ``origin/main``; evidence never reads a worktree."""

    repo: Repo
    commit: str

    @classmethod
    def of(cls, repo: Repo) -> MainTree:
        return cls(repo, Repo.git(repo.checkout, "rev-parse", "origin/main").strip())

    def has(self, path: str) -> bool:
        return self.git("cat-file", "-e", f"{self.commit}:{path}").returncode == 0

    def count(self, pattern: str, paths: tuple[str, ...]) -> int:
        """Matching lines under ``paths``; git pathspecs such as ``:!file`` exclude."""
        run = self.git("grep", "-c", "-E", "-e", pattern, self.commit, "--", *paths)
        if run.returncode not in (0, 1):
            run.check_returncode()
        return sum(int(line.rsplit(":", 1)[1]) for line in run.stdout.splitlines())

    def contains(self, sha: str) -> bool:
        return self.repo.contains(sha, self.commit)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", "-C", str(self.repo.checkout), *args),
                              capture_output=True, text=True, timeout=60)


@dataclass(frozen=True)
class PlanReading:
    """One look at the plans: the order history and the plan repository's main.

    Each evidence is read once per reading, however many phases wait on it."""

    todos: tuple[Todo, ...]
    main: MainTree
    found: dict[PhaseEvidence, bool] = field(default_factory=dict, compare=False, repr=False)

    def holds(self, evidence: PhaseEvidence) -> bool:
        if evidence not in self.found:
            self.found[evidence] = evidence.holds(self)
        return self.found[evidence]


class PhaseEvidence(ABC):
    @abstractmethod
    def holds(self, reading: PlanReading) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True)
class NoDeclaredEvidence(PhaseEvidence):
    """A phase whose plan names nothing checkable on main is never done by declaration."""

    def holds(self, reading: PlanReading) -> bool:
        return False

    def describe(self) -> str:
        return "no evidence declared; declare what main must show"


@dataclass(frozen=True)
class CommitMerged(PhaseEvidence):
    sha: str

    def holds(self, reading: PlanReading) -> bool:
        return reading.main.contains(self.sha)

    def describe(self) -> str:
        return f"{self.sha} on main"


@dataclass(frozen=True)
class PathsAbsent(PhaseEvidence):
    paths: tuple[str, ...]

    def holds(self, reading: PlanReading) -> bool:
        return not any(reading.main.has(path) for path in self.paths)

    def describe(self) -> str:
        return "absent from main: " + ", ".join(self.paths)


@dataclass(frozen=True)
class PatternEvidence(PhaseEvidence):
    """A ``git grep -E`` count on main, judged by the member."""

    pattern: str
    paths: tuple[str, ...] = ("src",)

    def holds(self, reading: PlanReading) -> bool:
        return self.satisfied(reading.main.count(self.pattern, self.paths))

    @abstractmethod
    def satisfied(self, count: int) -> bool: ...


@dataclass(frozen=True)
class PatternAbsent(PatternEvidence):
    def satisfied(self, count: int) -> bool:
        return count == 0

    def describe(self) -> str:
        return f"no {self.pattern!r} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class PatternPresent(PatternEvidence):
    def satisfied(self, count: int) -> bool:
        return count > 0

    def describe(self) -> str:
        return f"{self.pattern!r} in {' '.join(self.paths)} on main"


@dataclass(frozen=True)
class AllEvidence(PhaseEvidence):
    parts: tuple[PhaseEvidence, ...]

    def holds(self, reading: PlanReading) -> bool:
        return all(reading.holds(part) for part in self.parts)

    def describe(self) -> str:
        return "; ".join(part.describe() for part in self.parts)


@dataclass(frozen=True)
class ParityEvidence(PhaseEvidence):
    """The native/current comparison rows, which own the cutover decision."""

    def holds(self, reading: PlanReading) -> bool:
        return ParityRow.done(reading.todos)

    def describe(self) -> str:
        return "every parity row passes"
