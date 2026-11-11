from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Self

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import SurroundingChannel
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread, ThreadCommandAvailability


@dataclass(frozen=True, kw_only=True)
class ThreadBoundCliCommand(ThreadCommandAvailability, CliCommand):
    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus,
                        channel: SurroundingChannel, *,
                        catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls.thread_binding(thread),) if cls.available_for_thread(thread, status) else ()

    @classmethod
    @abstractmethod
    def thread_binding(cls, thread: Thread) -> Self: ...
