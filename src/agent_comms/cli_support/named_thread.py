from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_support.thread_bound import ThreadBoundCliCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import OneChannel
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel, ResolvedThread
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class NamedThreadCliCommand(ThreadBoundCliCommand, NamedCliCommand):
    @classmethod
    def thread_binding(cls, thread: Thread) -> Self:
        return cls(name=thread.name)

    @classmethod
    def channel_members(cls, resolved: ResolvedChannel) -> tuple[ResolvedThread, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return tuple(bound for member in cls.channel_members(resolved)
                     for bound in cls.thread_bindings(
                         comms, member.thread, member.status, OneChannel(resolved.channel.name), catalog=catalog))
