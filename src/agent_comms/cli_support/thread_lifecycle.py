from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from agent_comms.cli_verbs.cli_command import MultipleTargets
from agent_comms.registry.registry_document import ResolvedChannel, ResolvedThread


@dataclass(frozen=True, kw_only=True)
class ThreadLifecycleCliCommand(MultipleTargets, NamedThreadCliCommand):
    @classmethod
    def channel_members(cls, resolved: ResolvedChannel) -> tuple[ResolvedThread, ...]:
        return resolved.members
