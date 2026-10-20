"""The one record of this machine's commander facts, written by `agent-comms install`.

Every commander tool reads its paths, names and repositories from here; none of them
hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from ..comms import Comms
from ..errors import RelationViolationError
from ..field_codec import boundary_decoder
from ..install_choices import CommanderConfigFile, NoConfigFile, RepoChoice, WrittenConfigFile
from ..native_arguments import NativeArguments
from ..repo import Repo
from ..threads import Thread


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
    def packaged_spine() -> Path:
        return Path(str(files(__package__) / 'spine.md'))

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

    @staticmethod
    def homed(root: Path, home: Path) -> dict[str, Any]:
        """The fields an install derives from this machine; the commander and repos are recorded."""
        return {'root': root, 'state': home / '.local/state/agent-comms/commander',
                'worktrees': home / 'wt', 'scratch': home / '.cache/agent-scratch', 'disk': home}

    @classmethod
    def packaged_rules_files(cls) -> tuple[Path, ...]:
        """All packaged rule files, shared by review and session context."""
        rules = tuple(sorted(path for path in cls.packaged_rules().iterdir() if path.is_file()))
        if not rules:
            raise ValueError('The packaged commander rules directory is empty.')
        return rules

    def repo_directory(self, name: str) -> Path:
        """A configured repository name takes precedence over a directory spelling."""
        return next((repo.checkout for repo in self.repos
                     if repo.github.named(name)), Path(name)).resolve()

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
    def commanded_by(self, name: str, root: Path, home: Path) -> RecordedCommander: ...

    @abstractmethod
    def rehomed(self, root: Path, home: Path) -> CommanderRecord: ...

    @abstractmethod
    def with_repos(self, choice: RepoChoice) -> CommanderRecord: ...

    @abstractmethod
    def write(self, home: Path) -> CommanderConfigFile: ...


@dataclass(frozen=True)
class RecordedCommander(CommanderRecord):
    config: CommanderConfig

    def commanded_by(self, name: str, root: Path, home: Path) -> RecordedCommander:
        return RecordedCommander(self.config.model_copy(
            update={**CommanderConfig.homed(root, home), 'commander': name}))

    def rehomed(self, root: Path, home: Path) -> RecordedCommander:
        return RecordedCommander(self.config.model_copy(update=CommanderConfig.homed(root, home)))

    def with_repos(self, choice: RepoChoice) -> RecordedCommander:
        return RecordedCommander(self.config.model_copy(
            update={'repos': choice.applied(self.config.repos)}))

    def write(self, home: Path) -> WrittenConfigFile:
        return WrittenConfigFile(str(self.config.write(home)))


@dataclass(frozen=True)
class NoCommanderRecord(CommanderRecord):
    """The bus records no commander; an install names none, so it writes no config."""

    def commanded_by(self, name: str, root: Path, home: Path) -> RecordedCommander:
        return RecordedCommander(CommanderConfig(
            commander=name, repos=(), **CommanderConfig.homed(root, home)))

    def rehomed(self, root: Path, home: Path) -> NoCommanderRecord:
        return self

    def with_repos(self, choice: RepoChoice) -> NoCommanderRecord:
        return self

    def write(self, home: Path) -> NoConfigFile:
        return NoConfigFile()
