from __future__ import annotations

from agent_comms.channel_targets import SurroundingChannel

from abc import abstractmethod
from agent_comms.catalog_document import CatalogAccess
from agent_comms.cli_command import CliCommand, CommandBinding
from agent_comms.comms import Comms
from agent_comms.thread_execution import ThreadCommandAvailability
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ThreadBoundCliCommand(ThreadCommandAvailability, CliCommand):
    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus,
                        channel: SurroundingChannel, *,
                        catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        return (cls.thread_binding(thread),) if cls.available_for_thread(thread, status) else ()

    @classmethod
    @abstractmethod
    def thread_binding(cls, thread: Thread) -> CommandBinding: ...
