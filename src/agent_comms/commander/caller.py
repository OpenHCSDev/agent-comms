"""The commander a commander tool acts for: the registered thread whose process runs the tool."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ..active_route import resolve_comms_route
from ..child_process import ProcessIdentity
from ..comms import Comms
from ..errors import RelationViolationError
from ..thread_identity import ThreadIncarnation
from ..threads import Thread
from ..todos import TodoStore
from .config import CommanderConfig


def lineage(pid: int) -> tuple[ProcessIdentity, ...]:
    """The process and each ancestor, nearest first."""
    line: list[ProcessIdentity] = []
    while pid > 1:
        # Fields after comm's final ')' start at field 3, so ppid is the second.
        stat = Path(f"/proc/{pid}/stat").read_text()
        line.append(ProcessIdentity.capture(pid))
        pid = int(stat.rsplit(") ", 1)[1].split()[1])
    return tuple(line)


@dataclass(frozen=True)
class Commander:
    """The registered thread whose process runs the tool, itself or through an ancestor, tagged
    commander and holding its facts."""

    comms: Comms
    name: str
    config: CommanderConfig

    @classmethod
    def of(cls, comms: Comms, pid: int) -> Commander:
        threads = comms.registry.snapshot().threads.values()
        for identity in lineage(pid):
            for thread in threads:
                if thread.process_identity == identity:
                    return cls.registered(comms, thread)
        raise RelationViolationError(f"pid {pid} runs for no registered thread: no commander")

    @classmethod
    def registered(cls, comms: Comms, thread: Thread) -> Commander:
        if CommanderConfig.TAG not in thread.tags:
            raise RelationViolationError(
                f"{thread.name} is not a registered commander: its registration lacks the"
                f" {CommanderConfig.TAG} tag; run agent-comms install --commander {thread.name}"
            )
        return cls(comms, thread.name, thread.commander_config.config(thread.name))

    @classmethod
    @cache
    def calling(cls) -> Commander:
        """This process's commander, read once at its first dispatch."""
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
