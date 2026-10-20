"""The one record of this machine's commander facts, written by `agent-comms install`.

Every commander tool reads its paths, names and repositories from here; none of them
hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from ..comms import Comms
from ..errors import RelationViolationError
from ..native_arguments import NativeArguments
from ..repo import Repo
from ..threads import Thread


class CommanderRole:
    """The shared eligibility check for registration and every commander verb."""

    @staticmethod
    def require_commander(thread: Thread) -> None:
        if not NativeArguments(()).with_model(thread.model).provider_type.may_command:
            raise RelationViolationError(
                f"Thread {thread.name!r} with declared model {thread.model!r} "
                "cannot command: no Codex commander."
            )


class CommanderConfig(CommanderRole, BaseModel):
    model_config = ConfigDict(frozen=True)

    commander: str
    root: Path
    cli: Path
    state: Path
    board: Path
    narrative: Path
    repos: tuple[Repo, ...]
    worktrees: Path
    scratch: Path
    disk: Path
    parked: frozenset[str] = frozenset()

    @property
    def peer_sync_log(self) -> Path:
        return self.scratch / "peer-sync.log"

    @staticmethod
    def path(home: Path | None = None) -> Path:
        return (home or Path.home()) / '.config/agent-comms/commander.json'

    @staticmethod
    def packaged_rules() -> Path:
        return Path(str(files('agent_comms.commander') / 'rules'))

    @staticmethod
    def packaged_skill() -> Path:
        return Path(str(files('agent_comms.skills') / 'comms-commander/SKILL.md'))

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
    def packaged_rules_files(cls) -> tuple[Path, ...]:
        """All packaged rule files, shared by review and session context."""
        rules = tuple(sorted(path for path in cls.packaged_rules().iterdir() if path.is_file()))
        if not rules:
            raise ValueError('The packaged commander rules directory is empty.')
        return rules

    def repo_directory(self, name: str) -> Path:
        """A configured repository name takes precedence over a directory spelling."""
        return next((repo.checkout for repo in self.repos
                     if repo.github.rsplit('/', 1)[-1] == name), Path(name)).resolve()

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
                          self.scratch, self.board.parent):
            directory.mkdir(parents=True, exist_ok=True)
        return path
