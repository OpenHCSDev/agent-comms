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
from agent_comms.registry.threads import NativeOwnerCommand, NextTurnConfiguration, Thread


@dataclass(frozen=True, kw_only=True)
class ConfigureCliCommand(
    ModelContextArguments, NamedCliCommand, NextTurnConfiguration, NativeOwnerCommand,
    ThreadBoundCliCommand, ConfigurationCommand,
    TargetActionCommand[ThreadConfiguredResult], declared_name="configure"
):
    """Next-turn settings are a native owner's: `ThreadManagement.set_thread_configuration`
    writes a `NativeThread`, and its catalogs are its native runtime's. An external CLI picks
    its own model, so no menu offers this form for it (comms_model is native-only too)."""

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
        options = ConfigOptions.configured()
        asyncio.run(options.validate(candidate))
        self.apply_model_context(options.launch.configuration, candidate.model)
        updated = ctx.threads.set_thread_configuration(thread, self)
        return ThreadConfiguredResult(
            configured=updated.name,
            model=updated.model,
            thinking_level=updated.thinking_level,
            service_tier=updated.service_tier,
            context_edit_policy=updated.context_edit_policy,
        )
