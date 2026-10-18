"""Pi operations shared by RPC and Durable; each transport owns its queries."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, cast

from .agent_backend import (
    CompactionOutcome,
    Compacts,
    ConfiguresModel,
    ContextUsage,
    FailedCompaction,
    ReportsContext,
)

if TYPE_CHECKING:
    from .native_child import PiChild
    from .native_tool_call import NativeToolMode, ToolTransport
    from .pi_commands import Compact, PiCommand, SettingCommand
    from .pi_payloads import ModelsData, StateData
    from .threads import Thread


class PiCapabilities(Compacts, ReportsContext, ConfiguresModel):
    @staticmethod
    async def configure_tools(
        child: PiChild, mode: NativeToolMode, transport: ToolTransport
    ) -> None:
        from .pi_commands import ConfigureTools

        await child.request(ConfigureTools(tools=mode.tool_names, broker=transport.host_binding()))

    @abstractmethod
    async def inspect(self, thread: Thread, command: PiCommand) -> Any: ...

    def compaction_request(self, instructions: str | None) -> Compact:
        from .pi_commands import Compact

        return Compact(custom_instructions=instructions)

    async def compact(self, thread: Thread, instructions: str | None) -> CompactionOutcome:
        from .native_pi import NativePiUnavailable

        try:
            data = await self.inspect(thread, self.compaction_request(instructions))
            return cast(CompactionOutcome, data.outcome)
        except (NativePiUnavailable, ValueError) as error:
            return FailedCompaction(str(error))

    async def context_usage(self, thread: Thread) -> ContextUsage:
        from .pi_commands import GetSessionStats

        usage = (await self.inspect(thread, GetSessionStats())).context_usage
        return (
            ContextUsage(None, None)
            if usage is None
            else ContextUsage(usage.tokens, usage.context_window)
        )

    async def discover_models(self, thread: Thread) -> ModelsData:
        from .pi_commands import GetAvailableModels
        from .pi_payloads import ModelsData

        return cast(ModelsData, await self.inspect(thread, GetAvailableModels()))

    async def models(self, thread: Thread) -> tuple[str, ...]:
        data = await self.discover_models(thread)
        return tuple(
            dict.fromkeys(name for model in data.models if (name := model.display_name) is not None)
        )

    async def apply_settings(self, thread: Thread, commands: list[SettingCommand]) -> StateData:
        from .pi_commands import GetState
        from .pi_payloads import StateData

        for command in commands:
            await self.inspect(thread, command)
        return cast(StateData, await self.inspect(thread, GetState()))

    async def configure(
        self, thread: Thread, model: str | None, thinking: str | None
    ) -> tuple[str, str]:
        from .native_pi import NativePiUnavailable
        from .pi_commands import SetModel, SetThinkingLevel

        commands: list[SettingCommand] = []
        if model is not None:
            commands.append(SetModel.from_name(model))
        if thinking is not None:
            commands.append(SetThinkingLevel(level=thinking))
        if not commands:
            raise ValueError("A configuration change names a model or a thinking level")
        state = await self.apply_settings(thread, commands)
        for command in commands:
            command.require_applied(state)
        selected, level = state.model.display_name, state.thinking_level.wire_value()
        if selected is None or level is None:
            raise NativePiUnavailable("Pi configuration did not report model and thinking level")
        return selected, level
