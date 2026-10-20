"""The one record of this machine's commander facts, written by `agent-comms install`.

Every commander tool reads its paths, names and repositories from here; none of them
hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict

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

    @staticmethod
    def path(home: Path | None = None) -> Path:
        return (home or Path.home()) / '.config/agent-comms/commander.json'

    @staticmethod
    def packaged_rules() -> Path:
        return Path(str(files('agent_comms.commander') / 'rules'))

    @staticmethod
    def packaged_skill() -> Path:
        return Path(str(files('agent_comms.skills') / 'comms-commander/SKILL.md'))

    @classmethod
    def read(cls, path: Path | str) -> CommanderConfig:
        return cls.model_validate_json(Path(path).read_text())

    @staticmethod
    def root_path(root: Path) -> Path:
        return root / 'commander.json'

    @classmethod
    def read_root(cls, root: Path | str) -> CommanderConfig:
        root = Path(root).resolve()
        return cls.read(cls.root_path(root)).model_copy(update={'root': root})

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
        for directory in (self.state, self.state / 'forks', self.state / 'orders', self.worktrees,
                          self.scratch, self.board.parent):
            directory.mkdir(parents=True, exist_ok=True)
        return path
