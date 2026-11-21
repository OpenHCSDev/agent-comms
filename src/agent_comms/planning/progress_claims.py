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

from agent_comms.execution.turn_context import FileProvenance
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.projects.repo import Repo
from agent_comms.registry.threads import Thread

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class ClaimScope(DeclaredFamily, affix="Scope"):
    """Where a claim's artifact must be found: the work the claim is evidence for.

    A claim used to be handed that place as a bare ``Path`` by each caller, so a goal report
    looked in the claimant's worktree while an order delivery looked in the problem's own
    checkout. A claim about work done in another repository could resolve in one of them and
    never the other, and nothing in the types could tell the two apart. A scope answers the
    question once, from the work it names; no caller computes a directory by hand.
    """

    @abstractmethod
    def directory(self, comms: Comms, claimant: Thread) -> Path: ...


@dataclass(frozen=True)
class ProblemScope(ClaimScope):
    """The checkout the problem's own order names, wherever that problem was worked."""

    problem: str

    def directory(self, comms: Comms, claimant: Thread) -> Path:
        del claimant
        return comms.goals.todos.get(self.problem).order.check.directory


@dataclass(frozen=True)
class ThreadScope(ClaimScope):
    """The claimant's own project worktree: work that belongs to no problem."""

    def directory(self, comms: Comms, claimant: Thread) -> Path:
        del comms
        return claimant.worktree.path


@dataclass(frozen=True)
class ProgressClaim(DeclaredFamily, affix="Claim"):
    @abstractmethod
    def verify(self, comms: Comms, claimant: Thread) -> tuple[str, ...]:
        """Verify the artifact, returning the claim's observation for this operation."""

    @classmethod
    def require_all(
        cls,
        claims: tuple[ProgressClaim, ...],
        comms: Comms,
        claimant: Thread,
    ) -> dict[ProgressClaim, tuple[str, ...]]:
        return {claim: claim.verify(comms, claimant) for claim in dict.fromkeys(claims)}

    @classmethod
    def require_claimed(cls, claims: tuple[ProgressClaim, ...], report: str) -> None:
        """A done or delivered report stands only on an attached claim, never on its prose."""
        if not claims:
            raise ValueError(
                f"A {report} report needs a typed claim with its verifiable artifact."
            )


@dataclass(frozen=True)
class MergedClaim(ProgressClaim):
    """A commit merged on origin. The commit alone names no repository, so the claim says
    which work it is evidence for; a merge is never verified in whatever checkout the caller
    happened to be holding."""

    commit: str
    scope: ClaimScope

    timeout: ClassVar[float] = 30.0

    def verify(self, comms: Comms, claimant: Thread) -> tuple[str, ...]:
        return self.origin_branches(self.scope.directory(comms, claimant))

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

    def verify(self, comms: Comms, claimant: Thread) -> tuple[str, ...]:
        del claimant
        comms.goals.todos.get(self.problem).order.require_checked(self.problem)
        return ()


@dataclass(frozen=True)
class WorkingClaim(ProgressClaim):
    screenshot: str

    def verify(self, comms: Comms, claimant: Thread) -> tuple[str, ...]:
        del comms, claimant
        if not self.screenshot.strip() or not Path(self.screenshot).is_file():
            raise ValueError(
                f"A working claim needs a screenshot path that exists; "
                f"{self.screenshot!r} does not."
            )
        return ()


@dataclass(frozen=True)
class FileClaim(ProgressClaim, declared_name=None):
    """A claimed file is absolute; each member owns how its bytes are interpreted."""

    source: FileProvenance

    def __post_init__(self) -> None:
        if not Path(self.source.path).is_absolute():
            raise ValueError("A file claim needs an absolute source path.")


@dataclass(frozen=True)
class ArtifactClaim(FileClaim):
    """A binary artifact is authenticated as bytes and is never decoded as report text."""

    def verify(self, comms: Comms, claimant: Thread) -> tuple[str, ...]:
        del comms, claimant
        self.source.verify()
        return (self.source.public_description(),)


@dataclass(frozen=True)
class ReportClaim(FileClaim):
    """A delivered report names its original bounded UTF-8 file and exact content digest."""

    def verify(self, comms: Comms, claimant: Thread) -> tuple[str, ...]:
        del claimant
        if not self.source.public_text(comms).strip():
            raise ValueError(f"A report claim needs a nonempty report: {self.source.path!r}.")
        return (self.source.public_description(),)
