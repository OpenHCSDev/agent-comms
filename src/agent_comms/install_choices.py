"""Who runs one install: a named unit, whose next install it plans, or nobody.

A left-out installer records nothing, so no install writes this machine's names into
another machine's bus.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.field_codec import BoundedText, SubclassUnion
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_text import AbsentText, GivenText, TextChoice
from agent_comms.todos import PlannedInstallDeadline


class InstallerChoice(SubclassUnion, TextChoice, affix="Installer"):
    """The unit running this install, named so the next install's plan names it, or none."""

    @abstractmethod
    def scratch(self, rule: StorageRule) -> Path:
        """Where this install keeps its worktree and practice copies."""

    @abstractmethod
    def plan_next(self, root: Path, now: float) -> None:
        """Record the next install this one plans."""


@dataclass(frozen=True, slots=True)
class NamedInstaller(GivenText, InstallerChoice):
    text: Annotated[str, BoundedText]

    def scratch(self, rule: StorageRule) -> Path:
        return rule.initial_roots(self.text).scratch

    def plan_next(self, root: Path, now: float) -> None:
        PlannedInstallDeadline.after(self.text, now).plan(root)


@dataclass(frozen=True, slots=True)
class UnnamedInstaller(AbsentText, InstallerChoice):
    """No unit runs this install; it plans no next install for anyone."""

    def scratch(self, rule: StorageRule) -> Path:
        return rule.scratch_base

    def plan_next(self, root: Path, now: float) -> None:
        pass
