"""What a unit's goal report or order delivery may claim, and the artifact Core verifies behind it.

A claim is a typed record, never a word in free text: merged names a commit that resolves on
origin, installed names the order whose check result is recorded, working names a screenshot
that exists. A claim whose artifact does not verify is refused with that artifact named.
"""

from __future__ import annotations

import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .comms import Comms


@dataclass(frozen=True)
class ProgressClaim(DeclaredFamily, affix="Claim"):
    @abstractmethod
    def verify(self, comms: Comms, worktree: Path) -> None:
        """Raise ValueError naming the missing artifact unless it verifies."""

    @classmethod
    def require_all(cls, claims: tuple[ProgressClaim, ...], comms: Comms, worktree: Path) -> None:
        for claim in claims:
            claim.verify(comms, worktree)


@dataclass(frozen=True)
class MergedClaim(ProgressClaim):
    commit: str

    def verify(self, comms: Comms, worktree: Path) -> None:
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "branch",
                "--remotes",
                "--contains",
                self.commit,
                "--list",
                "origin/*",
            ],
            capture_output=True,
            text=True,
        )
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
class WorkingClaim(ProgressClaim):
    screenshot: str

    def verify(self, comms: Comms, worktree: Path) -> None:
        if not self.screenshot.strip() or not Path(self.screenshot).is_file():
            raise ValueError(
                f"A working claim needs a screenshot path that exists; "
                f"{self.screenshot!r} does not."
            )
