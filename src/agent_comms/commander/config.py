"""The commander facts a commander thread's registration holds, recorded by `agent-comms install`.

Every commander tool reads its board, paths and repositories from the registration of the
thread calling it (see `caller`); none of them hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

import subprocess
from abc import abstractmethod
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Annotated, ClassVar

from ..errors import RelationViolationError
from ..field_codec import NullMember, PathText, RecordMember, SubclassUnion
from ..registration_inheritance import RegistrationInheritance


@dataclass(frozen=True)
class Repo:
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    checkout: Annotated[Path, PathText]
    github: str
    source: str = 'src'

    @classmethod
    def of(cls, checkout: Path) -> Repo:
        """Its GitHub name comes from the checkout's own origin remote."""
        url = subprocess.run(['git', '-C', str(checkout), 'remote', 'get-url', 'origin'],
                             capture_output=True, text=True, check=True).stdout.strip()
        github = url.removesuffix('.git').replace(':', '/').split('/')[-2:]
        return cls(checkout=checkout.resolve(), github='/'.join(github))


class CommanderFacts(SubclassUnion, RegistrationInheritance["CommanderFacts"], affix=""):
    """What a thread's registration holds to command with."""

    @classmethod
    def apply(cls, incoming: CommanderFacts, previous: CommanderFacts) -> CommanderFacts:
        return incoming.registered(previous)

    @abstractmethod
    def registered(self, previous: CommanderFacts) -> CommanderFacts:
        """The facts a new declaration of the thread keeps."""

    @abstractmethod
    def config(self, thread: str) -> CommanderConfig: ...


@dataclass(frozen=True)
class NoCommanderFacts(NullMember, CommanderFacts):
    """A thread that commands nothing; declaring a thread again without facts keeps its recorded ones."""

    def registered(self, previous: CommanderFacts) -> CommanderFacts:
        return previous

    def config(self, thread: str) -> CommanderConfig:
        raise RelationViolationError(f'{thread} holds no commander facts; run agent-comms install --commander {thread}')


@dataclass(frozen=True)
class CommanderConfig(RecordMember, CommanderFacts):
    """What one commander works with, held on its own thread's registration."""

    TAG: ClassVar[str] = 'commander'

    cli: Annotated[Path, PathText]
    state: Annotated[Path, PathText]
    board: Annotated[Path, PathText]
    narrative: Annotated[Path, PathText]
    skill: Annotated[Path, PathText]
    rules: tuple[Annotated[Path, PathText], ...]
    repos: tuple[Repo, ...]
    worktrees: Annotated[Path, PathText]
    scratch: Annotated[Path, PathText]
    disk: Annotated[Path, PathText]
    parked: frozenset[str] = frozenset()

    @staticmethod
    def packaged_rules() -> Path:
        return Path(str(files('agent_comms.commander') / 'rules'))

    def registered(self, previous: CommanderFacts) -> CommanderFacts:
        return self

    def config(self, thread: str) -> CommanderConfig:
        return self

    def prepare(self) -> None:
        """Start each state file empty, so every tool reads a real file."""
        for directory in (self.state, self.state / 'forks', self.state / 'orders', self.worktrees,
                          self.scratch, self.board.parent):
            directory.mkdir(parents=True, exist_ok=True)
        if not (self.state / 'activity.log').exists():
            (self.state / 'activity.log').write_text('')
