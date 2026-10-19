"""The commander facts a commander thread's registration holds, recorded by `agent-comms install`.

Every commander tool reads its board, paths and repositories from the registration of the
thread calling it; none of them hardcodes a home directory, a bus root or a unit name.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from ..child_process import ProcessIdentity
from ..errors import RelationViolationError
from ..field_codec import PathText

if TYPE_CHECKING:
    from ..comms import Comms
    from ..thread_identity import ThreadIncarnation
    from ..todos import TodoStore


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


@dataclass(frozen=True)
class CommanderConfig:
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

    def prepare(self) -> None:
        """Start each state file empty, so every tool reads a real file."""
        for directory in (self.state, self.state / 'forks', self.state / 'orders', self.worktrees,
                          self.scratch, self.board.parent):
            directory.mkdir(parents=True, exist_ok=True)
        if not (self.state / 'activity.log').exists():
            (self.state / 'activity.log').write_text('')


def lineage(pid: int) -> tuple[ProcessIdentity, ...]:
    """The process and each ancestor, nearest first."""
    line: list[ProcessIdentity] = []
    while pid > 1:
        # Fields after comm's final ')' start at field 3, so ppid is the second.
        stat = Path(f'/proc/{pid}/stat').read_text()
        line.append(ProcessIdentity.capture(pid))
        pid = int(stat.rsplit(') ', 1)[1].split()[1])
    return tuple(line)


@dataclass(frozen=True)
class Commander:
    """The commander a tool acts for: the registered thread whose process runs the tool, itself or
    through an ancestor, tagged commander and holding its facts."""

    comms: Comms
    name: str
    config: CommanderConfig

    @classmethod
    def of(cls, comms: Comms, pid: int) -> Commander:
        threads = comms.registry.snapshot().threads.values()
        caller = next((thread for identity in lineage(pid) for thread in threads
                       if thread.process_identity == identity), None)
        if caller is None:
            raise RelationViolationError(f'pid {pid} runs for no registered thread, so no commander')
        if CommanderConfig.TAG not in caller.tags or caller.commander_config is None:
            raise RelationViolationError(
                f'{caller.name} is not a registered commander: its registration lacks the '
                f'{CommanderConfig.TAG} tag or its facts; run agent-comms install --commander NAME')
        return cls(comms, caller.name, caller.commander_config)

    @classmethod
    @cache
    def calling(cls) -> Commander:
        """This process's commander, read once at its first dispatch."""
        from ..active_route import resolve_comms_route
        from ..comms import Comms

        return cls.of(Comms(resolve_comms_route()), os.getpid())

    @property
    def todos(self) -> TodoStore:
        """The Todo store that owns problems, orders, deadlines and strikes."""
        return self.comms.goals.todos

    @property
    def incarnation(self) -> ThreadIncarnation:
        return self.comms.registry.require(self.name).incarnation


def me() -> Commander:
    """The commander this tool acts for, read from its registration at dispatch."""
    return Commander.calling()
