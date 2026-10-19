"""The one record of this machine's commander facts, written by `agent-comms install`.

Every commander tool reads its paths, names and repositories from here; none of them
hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..comms import Comms
from ..errors import RelationViolationError
from ..native_arguments import NativeArguments
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


class Repo(BaseModel):
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    model_config = ConfigDict(frozen=True)

    checkout: Path
    github: str
    source: str = 'src'

    @classmethod
    def of(cls, checkout: Path) -> Repo:
        """Its GitHub name comes from the checkout's own origin remote."""
        url = subprocess.run(['git', '-C', str(checkout), 'remote', 'get-url', 'origin'],
                             capture_output=True, text=True, check=True).stdout.strip()
        github = url.removesuffix('.git').replace(':', '/').split('/')[-2:]
        return cls(checkout=checkout.resolve(), github='/'.join(github))


class CommanderConfig(CommanderRole, BaseModel):
    model_config = ConfigDict(frozen=True)

    commander: str
    root: Path
    cli: Path
    state: Path
    board: Path
    narrative: Path
    skill: Path
    rules: tuple[Path, ...]
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

    @classmethod
    @cache
    def installed(cls) -> CommanderConfig:
        return cls.model_validate_json(cls.path().read_text())

    def write(self, home: Path) -> Path:
        """Record the config and start each state file empty, so every tool reads a real file."""
        self.require_commander(Comms(self.root).registry.require(self.commander))
        path = self.path(home)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(self.model_dump_json(indent=1) + '\n')
        for directory in (self.state, self.state / 'forks', self.state / 'orders', self.worktrees,
                          self.scratch, self.board.parent):
            directory.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(frozen=True)
class Commander:
    """This machine's commander thread and the bus whose Todo store owns its problems and orders."""

    name: str
    comms: Comms

    @classmethod
    def of(cls, config: CommanderConfig) -> Commander:
        return cls(config.commander, Comms(config.root))

    def units(self) -> frozenset[str]:
        """The threads this commander gives orders to; the owner, claude-ai and peer commanders
        are not among them."""
        return self.comms.goals.todos.units(self.comms.registry.require(self.name).incarnation)
