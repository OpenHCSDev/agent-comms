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
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .comms import Comms


@dataclass(frozen=True)
class ProgressClaim(DeclaredFamily, affix="Claim"):
    @abstractmethod
    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        """Verify the artifact, returning the claim's observation for this operation."""

    @classmethod
    def require_all(
        cls, claims: tuple[ProgressClaim, ...], comms: Comms, worktree: Path,
    ) -> dict[ProgressClaim, tuple[str, ...]]:
        return {claim: claim.verify(comms, worktree) for claim in dict.fromkeys(claims)}

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

    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        return self.origin_branches(worktree)

    def origin_branches(self, worktree: Path) -> tuple[str, ...]:
        command = [
            "git",
            "-C",
            str(worktree),
            "for-each-ref",
            f"--contains={self.commit}",
            "--format=%(refname:strip=3)",
            "refs/remotes/origin/",
        ]
        try:
            listed = subprocess.run(
                command,
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
        return tuple(listed.stdout.splitlines())


@dataclass(frozen=True)
class InstalledClaim(ProgressClaim):
    problem: str

    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        comms.goals.todos.get(self.problem).order.require_checked(self.problem)
        return ()


@dataclass(frozen=True)
class WorkingClaim(ProgressClaim):
    screenshot: str

    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        if not self.screenshot.strip() or not Path(self.screenshot).is_file():
            raise ValueError(
                f"A working claim needs a screenshot path that exists; "
                f"{self.screenshot!r} does not."
            )
        return ()
