"""What one install is told about its bus: the commander, its repositories and who runs it.

Each is given on the command line or left out; a left-out choice keeps what the bus already
records, so no install writes this machine's names into another machine's bus.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from agent_comms.field_codec import (
    BoundedText, FieldCodec, NullMember, SingleFieldValue, SubclassUnion,
)
from agent_comms.thread_text import AbsentText, GivenText, TextChoice

if TYPE_CHECKING:
    from agent_comms.commander.config import CommanderRecord
    from agent_comms.repo import Repo
    from agent_comms.storage_rules import StorageRule


class CommanderChoice(SubclassUnion, TextChoice, affix="Commander"):
    """The thread an install names as this bus's commander, or none: the bus's record stands."""

    @abstractmethod
    def configured(self, recorded: CommanderRecord, root: Path, home: Path) -> CommanderRecord: ...


@dataclass(frozen=True, slots=True)
class NamedCommander(GivenText, CommanderChoice):
    text: Annotated[str, BoundedText]

    def configured(self, recorded: CommanderRecord, root: Path, home: Path) -> CommanderRecord:
        return recorded.commanded_by(self.text, root, home)


@dataclass(frozen=True, slots=True)
class KeptCommander(AbsentText, CommanderChoice):
    def configured(self, recorded: CommanderRecord, root: Path, home: Path) -> CommanderRecord:
        return recorded.rehomed(root, home)


class RepoChoice(SubclassUnion, affix="Repos"):
    """The repositories the commander works on: the checkouts given, or the recorded ones."""

    @abstractmethod
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]: ...


@dataclass(frozen=True, slots=True)
class GivenRepos(RepoChoice):
    """Checkouts spelled as comma-separated paths on the command line."""

    checkouts: tuple[Path, ...]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        text = FieldCodec.decode(str, data)
        return cls(tuple(Path(part.strip()) for part in text.split(",") if part.strip()))

    def to_wire(self) -> str:
        return ",".join(map(str, self.checkouts))

    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        from agent_comms.repo import Repo

        return tuple(Repo.of(checkout) for checkout in self.checkouts)


@dataclass(frozen=True, slots=True)
class KeptRepos(NullMember, RepoChoice):
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        return recorded


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
        from agent_comms.todos import PlannedInstallDeadline

        PlannedInstallDeadline.after(self.text, now).plan(root)


@dataclass(frozen=True, slots=True)
class UnnamedInstaller(AbsentText, InstallerChoice):
    """No unit runs this install; it plans no next install for anyone."""

    def scratch(self, rule: StorageRule) -> Path:
        return rule.scratch_base

    def plan_next(self, root: Path, now: float) -> None:
        pass


class CommanderConfigFile(SubclassUnion, affix="ConfigFile"):
    """What an install left for commander tools: the config it wrote, or none."""


@dataclass(frozen=True, slots=True)
class WrittenConfigFile(SingleFieldValue, CommanderConfigFile):
    path: str


@dataclass(frozen=True, slots=True)
class NoConfigFile(NullMember, CommanderConfigFile):
    pass
