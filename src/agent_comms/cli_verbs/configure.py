from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_support.thread_bound import ThreadBoundCliCommand
from agent_comms.cli_verbs.cli_command import ConfigurationCommand, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import ThreadConfiguredResult
from agent_comms.cli_verbs.model_context import ModelContextArguments
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions
from agent_comms.registry.thread_model import UndeclaredModel
from agent_comms.registry.threads import ConfigurableThreadCommand, NextTurnConfiguration, Thread


@dataclass(frozen=True, kw_only=True)
class ConfigureCliCommand(
    ModelContextArguments, NamedCliCommand, NextTurnConfiguration, ConfigurableThreadCommand,
    ThreadBoundCliCommand, ConfigurationCommand,
    TargetActionCommand[ThreadConfiguredResult], declared_name="configure"
):
    """Next-turn settings and execution mode (native Pi model or CLI inbox) for a thread."""

    help = "Set a thread's model, thinking level, or service tier for its next turn"
    menu_label = "Configure"

    @classmethod
    def thread_binding(cls, thread: Thread) -> ConfigureCliCommand:
        return cls(name=thread.name)

    def catalog_selection(self, catalogs: ConfigOptions) -> object:
        """Discover against this thread after applying the form's proposed changes."""
        with Comms.client(catalogs.route) as comms:
            selection = comms.threads.running_selection(self.name)
            return self.configured(replace(selection.thread, model=selection.model))

    def apply(self, ctx: Comms) -> ThreadConfiguredResult:
        thread = ctx.registry.require(self.name)
        candidate = self.configured(thread)
        options = ConfigOptions.for_route(ctx.route)
        asyncio.run(options.validate(candidate))
        self.apply_model_context(options.launch.configuration, candidate.model)
        updated = self.model.apply_to_thread(ctx.threads, thread, self)
        return ThreadConfiguredResult(
            configured=updated.name,
            model=updated.catalog_model(UndeclaredModel()),
            thinking_level=updated.thinking_level,
            service_tier=updated.service_tier,
            context_edit_policy=updated.context_edit_policy,
        )
