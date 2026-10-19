"""What a unit's goal report or order delivery may claim, and the artifact Core verifies behind it.

A claim is a typed record, never a word in free text: merged names a commit that resolves on
origin, installed names the order whose check result is recorded, working names a screenshot
that exists, measured quotes the command a report ran and the count it printed. A claim whose
artifact does not verify is refused with that artifact named.
"""

from __future__ import annotations

import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .goals import CountCheckResult, OrderCheck

if TYPE_CHECKING:
    from .comms import Comms


@dataclass(frozen=True)
class ProgressClaim(DeclaredFamily, affix="Claim"):
    @abstractmethod
    def verify(self, comms: Comms, worktree: Path) -> None:
        """Raise ValueError naming the missing artifact unless it verifies."""

    def measures(self, check: OrderCheck) -> None:
        """Raise ValueError unless this claim measured what the order's Check declares."""

    @classmethod
    def require_all(cls, claims: tuple[ProgressClaim, ...], comms: Comms, worktree: Path) -> None:
        for claim in claims:
            claim.verify(comms, worktree)

    @classmethod
    def require_measured(cls, claims: tuple[ProgressClaim, ...], check: OrderCheck) -> None:
        """A done report's measure is its order's Check, never one the report chose."""
        for claim in claims:
            claim.measures(check)

    @classmethod
    def require_claimed(cls, claims: tuple[ProgressClaim, ...], report: str) -> None:
        """A done or delivered report stands only on an attached claim, never on its prose."""
        if not claims:
            raise ValueError(
                f"A {report} report needs a claim with its artifact: merged (a commit "
                "on origin), installed (a recorded check) or working (a screenshot)."
            )


@dataclass(frozen=True)
class MergedClaim(ProgressClaim):
    commit: str

    timeout: ClassVar[float] = 30.0

    def verify(self, comms: Comms, worktree: Path) -> None:
        command = ["git", "-C", str(worktree), "branch", "--remotes", "--contains", self.commit]
        try:
            listed = subprocess.run(
                [*command, "--list", "origin/*"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as expired:
            raise ValueError(
                f"A merged claim needs a commit sha that resolves on origin; looking up "
                f"{self.commit!r} took longer than {expired.timeout:g}s."
            ) from expired
        if not self.commit.strip() or listed.returncode or not listed.stdout.strip():
            raise ValueError(
                f"A merged claim needs a commit sha that resolves on origin; "
                f"{self.commit!r} does not."
            )


@dataclass(frozen=True)
class InstalledClaim(ProgressClaim):
    problem: str

    def verify(self, comms: Comms, worktree: Path) -> None:
        comms.goals.todos.get(self.problem).order.require_checked(self.problem)


@dataclass(frozen=True)
class MeasuredClaim(ProgressClaim):
    """What a report ran and the count it printed; the order's Check stays the measure."""

    command: str
    count: int

    def verify(self, comms: Comms, worktree: Path) -> None:
        """Its artifact is the order's Check result, which the delivery records."""

    def measures(self, check: OrderCheck) -> None:
        if (self.command, CountCheckResult(self.count)) != (check.command, check.result):
            raise ValueError(
                f"The report measured {self.command!r} -> {self.count}; the order's Check is "
                f"{check.command!r} -> {check.result}. Report the Check's result at the "
                "delivered head."
            )


@dataclass(frozen=True)
class WorkingClaim(ProgressClaim):
    screenshot: str

    def verify(self, comms: Comms, worktree: Path) -> None:
        if not self.screenshot.strip() or not Path(self.screenshot).is_file():
            raise ValueError(
                f"A working claim needs a screenshot path that exists; "
                f"{self.screenshot!r} does not."
            )
