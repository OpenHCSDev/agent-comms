from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument, ThreadConfiguredResult
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions
from agent_comms.registry.threads import NextTurnConfiguration


@dataclass(frozen=True, kw_only=True)
class ConfigureCliCommand(
    NameArgument, NextTurnConfiguration, CliCommand, declared_name="configure"
):
    help = "Set a thread's model, thinking level, or service tier for its next turn"

    def apply(self, ctx: Comms) -> CliResult:
        thread = ctx.registry.require(self.name)
        asyncio.run(ConfigOptions.configured().validate(self.configured(thread)))
        updated = ctx.threads.set_thread_configuration(thread, self)
        return ThreadConfiguredResult(updated.name, updated.model, updated.thinking_level,
                                      updated.service_tier)
