from __future__ import annotations

from agent_comms.registry_document import ResolvedChannel, ResolvedThread
from agent_comms.cli_command import MultipleTargets
from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ThreadLifecycleCliCommand(MultipleTargets, NamedThreadCliCommand):
    @classmethod
    def channel_members(cls, resolved: ResolvedChannel) -> tuple[ResolvedThread, ...]:
        return resolved.members
