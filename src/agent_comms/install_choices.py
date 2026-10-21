"""Who runs one install, selecting its scratch directory without scheduling another."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.field_codec import BoundedText, SubclassUnion
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_text import AbsentText, GivenText, TextChoice


class InstallerChoice(SubclassUnion, TextChoice, affix="Installer"):
    """The unit running this install, or none."""

    @abstractmethod
    def scratch(self, rule: StorageRule) -> Path:
        """Where this install keeps its worktree and practice copies."""


@dataclass(frozen=True, slots=True)
class NamedInstaller(GivenText, InstallerChoice):
    text: Annotated[str, BoundedText]

    def scratch(self, rule: StorageRule) -> Path:
        return rule.initial_roots(self.text).scratch


@dataclass(frozen=True, slots=True)
class UnnamedInstaller(AbsentText, InstallerChoice):
    def scratch(self, rule: StorageRule) -> Path:
        return rule.scratch_base
