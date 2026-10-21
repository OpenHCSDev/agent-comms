"""The one record of this machine's commander facts, written by `agent-comms install`.

Every commander tool reads its paths, names and repositories from here; none of them
hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from agent_comms.phase_evidence import MainTree, PlanReading
from agent_comms.plan_phases import PlanPhase

from ..comms import Comms
from ..errors import RelationViolationError
from ..field_codec import (
    BoundedText,
    FieldCodec,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
    boundary_decoder,
)
from ..installer.backup import UpgradeRefused
from ..native_arguments import NativeArguments
from ..repo import Repo
from ..thread_text import AbsentText, GivenText, TextChoice
from ..threads import Thread

if TYPE_CHECKING:
    from agent_comms.todos import Todo


class CommanderRole:
    """The shared eligibility check for registration and every commander verb."""

    @staticmethod
    def require_commander(thread: Thread) -> None:
        if not thread.model.native(NativeArguments(())).provider_type.may_command:
            raise RelationViolationError(
                f"Thread {thread.name!r} with declared model {thread.model.otherwise(None)!r} "
                "cannot command: no Codex commander."
            )


class CommanderConfig(CommanderRole, BaseModel):
    model_config = ConfigDict(frozen=True)

    commander: str
    root: Path
    state: Path
    repos: tuple[Repo, ...]
    worktrees: Path
    scratch: Path
    disk: Path
    parked: frozenset[str] = frozenset()

    @staticmethod
    def path(home: Path | None = None) -> Path:
        return (home or Path.home()) / '.config/agent-comms/commander.json'

    @staticmethod
    def packaged_rules() -> Path:
        return Path(str(files(__package__) / 'rules'))

    @staticmethod
    def packaged_skill() -> Path:
        return Path(str(files('agent_comms.skills') / 'comms-commander/SKILL.md'))

    @staticmethod
    def packaged_indictment() -> Path:
        return Path(str(files(__package__) / 'indictment.md'))


    @staticmethod
    def packaged_voice() -> Path:
        return Path(str(files(__package__) / 'voice.md'))

    @field_validator('root')
    @classmethod
    def selected_root(cls, root: Path, info: ValidationInfo) -> Path:
        """Select the root before a context's post-init can open any stores."""
        return info.context if isinstance(info.context, Path) else root

    @classmethod
    def read(cls, path: Path | str) -> Self:
        return cls.model_validate_json(Path(path).read_text())

    @staticmethod
    def root_path(root: Path) -> Path:
        return root / 'commander.json'

    @classmethod
    def read_root(cls, root: Path | str) -> Self:
        root = Path(root).resolve()
        return cls.model_validate_json(cls.root_path(root).read_text(), context=root)

    @classmethod
    def on_machine(cls, commander: str, repos: tuple[Repo, ...], root: Path, home: Path,
                   parked: frozenset[str] = frozenset()) -> Self:
        """The recorded commander and repos, with the paths this machine derives."""
        return cls(commander=commander, repos=repos, parked=parked, root=root,
                   state=home / '.local/state/agent-comms/commander', worktrees=home / 'wt',
                   scratch=home / '.cache/agent-scratch', disk=home)

    @classmethod
    def packaged_rules_files(cls) -> tuple[Path, ...]:
        """All packaged rule files, shared by review and session context."""
        rules = tuple(sorted(path for path in cls.packaged_rules().iterdir() if path.is_file()))
        if not rules:
            raise ValueError('The packaged commander rules directory is empty.')
        return rules

    def repo_directory(self, name: str) -> Path:
        """Only the configured repositories can supply an order's check checkout."""
        for repo in self.repos:
            if repo.named(name):
                return repo.checkout
        configured = ', '.join(f'{repo.github.to_wire()} ({repo.checkout})' for repo in self.repos)
        raise ValueError(
            f'Unknown check repository {name!r}. Configured repositories: {configured}. '
            'Rerun agent-comms install with --repos to configure the checkouts.'
        )

    def plan_reading(self, todos: tuple[Todo, ...]) -> PlanReading:
        """The plans, read against main of the repository that declares them."""
        return PlanReading(todos, MainTree.of(self.module_repo(PlanPhase.__module__)))

    def module_repo(self, module: str) -> Repo:
        """The configured repository whose checkout holds ``module``; none is an error."""
        for repo in self.repos:
            if (repo.checkout / repo.module_file(module)).is_file():
                return repo
        raise ValueError(f'No configured repository holds {module}')

    def write(self, home: Path) -> Path:
        """Record the config and start each state file empty, so every tool reads a real file."""
        self.require_commander(Comms(self.root).registry.require(self.commander))
        path = self.path(home)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = self.root_path(self.root)
        record.write_text(self.model_dump_json(indent=1) + '\n')
        path.unlink(missing_ok=True)
        path.symlink_to(record)
        for directory in (self.state, self.state / 'orders', self.worktrees,
                          self.scratch):
            directory.mkdir(parents=True, exist_ok=True)
        return path


class CommanderRecord(ABC):
    """This bus's commander.json, or the bus has none; an install updates it through here."""

    @staticmethod
    @boundary_decoder
    def read(root: Path) -> CommanderRecord:
        root = root.resolve()
        if CommanderConfig.root_path(root).is_file():
            return RecordedCommander(CommanderConfig.read_root(root))
        return NoCommanderRecord()

    @abstractmethod
    def commanded_by(self, name: str, repos: RepoChoice, root: Path, home: Path) -> RecordedCommander: ...

    @abstractmethod
    def rehomed(self, repos: RepoChoice, root: Path, home: Path) -> CommanderRecord: ...

    @abstractmethod
    def write(self, home: Path) -> CommanderConfigFile: ...


@dataclass(frozen=True)
class RecordedCommander(CommanderRecord):
    config: CommanderConfig

    def commanded_by(self, name: str, repos: RepoChoice, root: Path, home: Path) -> RecordedCommander:
        return RecordedCommander(CommanderConfig.on_machine(
            name, repos.applied(self.config.repos), root, home, self.config.parked))

    def rehomed(self, repos: RepoChoice, root: Path, home: Path) -> RecordedCommander:
        return self.commanded_by(self.config.commander, repos, root, home)

    def write(self, home: Path) -> WrittenConfigFile:
        return WrittenConfigFile(str(self.config.write(home)))


@dataclass(frozen=True)
class NoCommanderRecord(CommanderRecord):
    """The bus records no commander; an install names none, so it writes no config."""

    def commanded_by(self, name: str, repos: RepoChoice, root: Path, home: Path) -> RecordedCommander:
        return repos.founding(name, root, home)

    def rehomed(self, repos: RepoChoice, root: Path, home: Path) -> NoCommanderRecord:
        repos.without_commander()
        return self

    def write(self, home: Path) -> NoConfigFile:
        return NoConfigFile()


class CommanderChoice(SubclassUnion, TextChoice, affix="Commander"):
    """The thread an install names as this bus's commander, or none: the bus's record stands."""

    @abstractmethod
    def configured(
        self, recorded: CommanderRecord, repos: RepoChoice, root: Path, home: Path,
    ) -> CommanderRecord: ...


@dataclass(frozen=True, slots=True)
class NamedCommander(GivenText, CommanderChoice):
    text: Annotated[str, BoundedText]

    def configured(
        self, recorded: CommanderRecord, repos: RepoChoice, root: Path, home: Path,
    ) -> CommanderRecord:
        return recorded.commanded_by(self.text, repos, root, home)


@dataclass(frozen=True, slots=True)
class KeptCommander(AbsentText, CommanderChoice):
    def configured(
        self, recorded: CommanderRecord, repos: RepoChoice, root: Path, home: Path,
    ) -> CommanderRecord:
        return recorded.rehomed(repos, root, home)


class RepoChoice(SubclassUnion, affix="Repos"):
    """The repositories the commander works on: the checkouts given, or the recorded ones."""

    @abstractmethod
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]: ...

    @abstractmethod
    def founding(self, commander: str, root: Path, home: Path) -> RecordedCommander:
        """The first record of a bus that had none: a commander needs repositories to command."""

    @abstractmethod
    def without_commander(self) -> None:
        """Refuse repositories given to a bus that records no commander to hold them."""

    @abstractmethod
    def resolved(self) -> Self:
        """This choice with its paths absolute, for an install child that runs elsewhere."""


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
        return tuple(Repo.of(checkout) for checkout in self.checkouts)

    def founding(self, commander: str, root: Path, home: Path) -> RecordedCommander:
        return RecordedCommander(CommanderConfig.on_machine(commander, self.applied(()), root, home))

    def without_commander(self) -> None:
        raise UpgradeRefused("--repos names a commander's repositories; this bus records no "
                             "commander, so name one with --commander")

    def resolved(self) -> Self:
        return type(self)(tuple(checkout.resolve() for checkout in self.checkouts))


@dataclass(frozen=True, slots=True)
class KeptRepos(NullMember, RepoChoice):
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]:
        return recorded

    def founding(self, commander: str, root: Path, home: Path) -> RecordedCommander:
        raise UpgradeRefused(f"--commander {commander} starts this bus's commander record, "
                             "which needs the repositories it commands: pass --repos")

    def without_commander(self) -> None:
        pass

    def resolved(self) -> Self:
        return self


class CommanderConfigFile(SubclassUnion, affix="ConfigFile"):
    """What an install left for commander tools: the config it wrote, or none."""


@dataclass(frozen=True, slots=True)
class WrittenConfigFile(SingleFieldValue, CommanderConfigFile):
    path: str


@dataclass(frozen=True, slots=True)
class NoConfigFile(NullMember, CommanderConfigFile):
    pass
