"""What a unit's goal report or order delivery may claim, and the artifact Core verifies behind it.

A claim is a typed record, never a word in free text. Each declared member owns the
artifact it authenticates; an artifact that does not verify is refused before completion.
"""

from __future__ import annotations

import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .nominal.declared_family import DeclaredFamily
from .repo import Repo
from .turn_context import FileProvenance

if TYPE_CHECKING:
    from .comms import Comms


@dataclass(frozen=True)
class ProgressClaim(DeclaredFamily, affix="Claim"):
    @abstractmethod
    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        """Verify the artifact, returning the claim's observation for this operation."""

    @classmethod
    def require_all(
        cls,
        claims: tuple[ProgressClaim, ...],
        comms: Comms,
        worktree: Path,
    ) -> dict[ProgressClaim, tuple[str, ...]]:
        return {claim: claim.verify(comms, worktree) for claim in dict.fromkeys(claims)}

    @classmethod
    def require_claimed(cls, claims: tuple[ProgressClaim, ...], report: str) -> None:
        """A done or delivered report stands only on an attached claim, never on its prose."""
        if not claims:
            raise ValueError(
                f"A {report} report needs a typed claim with its verifiable artifact."
            )


@dataclass(frozen=True)
class MergedClaim(ProgressClaim):
    commit: str

    timeout: ClassVar[float] = 30.0

    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        return self.origin_branches(worktree)

    def unresolved(self) -> ValueError:
        return ValueError(
            f"A merged claim needs a commit sha that resolves on origin; {self.commit!r} does not."
        )

    def origin_branches(self, worktree: Path) -> tuple[str, ...]:
        try:
            if not self.commit.strip() or not Repo.answers(
                worktree, "rev-parse", "--verify", "--quiet", f"{self.commit}^{{commit}}"
            ):
                raise self.unresolved()
            listed = Repo.git(
                worktree,
                "for-each-ref",
                f"--contains={self.commit}",
                "--format=%(refname:strip=3)",
                "refs/remotes/origin/",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as expired:
            raise ValueError(
                f"A merged claim needs a commit sha that resolves on origin; looking up "
                f"{self.commit!r} took longer than {expired.timeout:g}s."
            ) from expired
        if not listed.strip():
            raise self.unresolved()
        return tuple(listed.splitlines())


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


@dataclass(frozen=True)
class ReportClaim(ProgressClaim):
    """A delivered report names its original UTF-8 file and exact content digest."""

    source: FileProvenance

    def __post_init__(self) -> None:
        if not Path(self.source.path).is_absolute():
            raise ValueError("A report claim needs an absolute source path.")

    def verify(self, comms: Comms, worktree: Path) -> tuple[str, ...]:
        if not self.source.public_text(comms).strip():
            raise ValueError(f"A report claim needs a nonempty report: {self.source.path!r}.")
        return (self.source.public_description(),)
