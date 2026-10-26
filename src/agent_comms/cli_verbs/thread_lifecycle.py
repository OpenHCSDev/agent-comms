"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.channel_targets import ResolvedChannel
from agent_comms.channel_targets import ResolvedThread
from agent_comms.cli_commands import MultipleTargets
from agent_comms.cli_verbs.named_thread import NamedThreadCliCommand
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ThreadLifecycleCliCommand(MultipleTargets, NamedThreadCliCommand):
    @classmethod
    def channel_members(cls, resolved: ResolvedChannel) -> tuple[ResolvedThread, ...]:
        return resolved.members
