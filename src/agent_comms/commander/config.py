"""Install choices configure the native registry project; no commander file is read."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

from ..field_codec import AbsolutePathText, BoundedText, DeclaredWireType, FieldCodec, NullMember, SubclassUnion
from ..installer.backup import UpgradeRefused
from ..registry_document import CommanderProject, RegistrySnapshot
from ..repo import Repo, RepositorySelection
from ..thread_text import AbsentText, GivenText, TextChoice


class CommanderChoice(SubclassUnion, TextChoice, affix="Commander"):
    """An explicit commander or preservation of this bus's native project."""

    @abstractmethod
    def configured(self, recorded: CommanderProject, repos: RepoChoice,
                   snapshot: RegistrySnapshot) -> CommanderProject: ...


@dataclass(frozen=True, slots=True)
class NamedCommander(GivenText, CommanderChoice):
    text: Annotated[str, BoundedText]

    def configured(self, recorded: CommanderProject, repos: RepoChoice,
                   snapshot: RegistrySnapshot) -> CommanderProject:
        commander = snapshot.require(self.text)
        commander.require_commander()
        return recorded.named(commander.incarnation, repos)


@dataclass(frozen=True, slots=True)
class KeptCommander(AbsentText, CommanderChoice):
    def configured(self, recorded: CommanderProject, repos: RepoChoice,
                   snapshot: RegistrySnapshot) -> CommanderProject:
        return recorded.reconfigured(repos)


class RepoChoice(RepositorySelection, SubclassUnion, affix="Repos"):
    """Explicit repository checkouts or the project's current native repositories."""


@dataclass(frozen=True, slots=True)
class GivenRepos(DeclaredWireType, RepoChoice):
    wire_type = str  # comma-separated checkouts
    checkouts: tuple[Path, ...]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        text = FieldCodec.decode(cls.wire_type, data)
        return cls(tuple(AbsolutePathText.decode(part.strip())
                         for part in text.split(",") if part.strip()))

    def to_wire(self) -> str:
        return ",".join(map(str, self.checkouts))

    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        return tuple(Repo.of(checkout) for checkout in self.checkouts)

    def founding(self, commander: str) -> tuple[Repo, ...]:
        return self.applied(())

    def without_commander(self) -> None:
        raise UpgradeRefused("--repos names a commander's repositories; this bus records no "
                             "commander, so name one with --commander")


@dataclass(frozen=True, slots=True)
class KeptRepos(NullMember, RepoChoice):
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        return recorded

    def founding(self, commander: str) -> tuple[Repo, ...]:
        raise UpgradeRefused(f"--commander {commander} starts this bus's commander record, "
                             "which needs the repositories it commands: pass --repos")

    def without_commander(self) -> None:
        pass

