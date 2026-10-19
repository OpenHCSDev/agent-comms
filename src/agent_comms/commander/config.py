"""The one record of this machine's commander facts, written by `agent-comms install`.

Every commander tool reads its paths, names and repositories from here; none of them
hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

import subprocess
from functools import cache
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict


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


class CommanderConfig(BaseModel):
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
        path = self.path(home)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(self.model_dump_json(indent=1) + '\n')
        for directory in (self.state, self.state / 'forks', self.state / 'orders', self.worktrees,
                          self.scratch, self.board.parent):
            directory.mkdir(parents=True, exist_ok=True)
        return path
