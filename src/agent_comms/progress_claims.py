"""What a unit's goal report or order delivery may claim, and the artifact Core verifies behind it.

A claim is a typed record, never a word in free text: installed names the order whose check
result is recorded, working names a screenshot that exists. A claim whose artifact does not
verify is refused with that artifact named. Whether a commit is on origin is the fact its
CommitDelivery owns, never a claim.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from .declared_family import DeclaredFamily
from .field_codec import PathText

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

    @classmethod
    def require_claimed(cls, claims: tuple[ProgressClaim, ...]) -> None:
        """A completed report stands only on an attached claim, never on its prose."""
        if not claims:
            raise ValueError(
                "A completed report needs a claim with its artifact: installed (a recorded "
                "check) or working (a screenshot)."
            )


@dataclass(frozen=True)
class InstalledClaim(ProgressClaim):
    problem: str

    def verify(self, comms: Comms, worktree: Path) -> None:
        comms.goals.todos.get(self.problem).order.require_checked(self.problem)


@dataclass(frozen=True)
class WorkingClaim(ProgressClaim):
    screenshot: Annotated[Path, PathText]

    def verify(self, comms: Comms, worktree: Path) -> None:
        if not self.screenshot.is_file():
            raise ValueError(
                f"A working claim needs a screenshot path that exists; "
                f"{str(self.screenshot)!r} does not."
            )
