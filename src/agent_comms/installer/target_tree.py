"""The temporary worktree of the target revision an install runs from.

Installer-internal: the outgoing runtime imports it before the target's own runtime exists.
"""
from __future__ import annotations

import shutil
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from agent_comms.installer.carry import STEPS
from agent_comms.repo import DetachedStart, Repo, ScratchCheckout


@dataclass(frozen=True)
class ScratchTree(AbstractContextManager):
    """A revision's worktree in its own scratch directory, retired with it."""

    scratch: Path
    revision: str

    @classmethod
    def entered(cls) -> ScratchTree:
        """The launcher starts the install inside its tree (InstallRun.spawn): the scratch
        directory holds it and its checkout names the revision."""
        path = Path.cwd()
        return cls(path.parent, Repo.git(path, "rev-parse", "HEAD").strip())

    @property
    def path(self) -> Path:
        return self.scratch / f"tree-{self.revision[:12]}"

    @property
    def steps_package(self) -> Path:
        """Where the tree's carry steps import from; they ship in no runtime."""
        return self.path / STEPS.parent

    def source_paths(self) -> tuple[Path, Path]:
        """What the installer imports from when an older runtime runs it: the tree's
        ``src``, then its carry steps' package."""
        return self.path / "src", self.steps_package

    def close(self) -> None:
        """Retire the tree; a failed add left nothing to retire."""
        if self.path.is_dir():
            ScratchCheckout(self.path).retire()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()
        if self.scratch.exists():
            shutil.rmtree(self.scratch)


@dataclass(frozen=True, kw_only=True)
class TargetTree(ScratchTree):
    """The tree the launcher adds from the operator's checkout."""

    repo: Path

    def add(self) -> None:
        ScratchCheckout.add(self.repo, self.path, DetachedStart(self.revision))
