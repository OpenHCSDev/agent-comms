"""Pi operations shared by RPC and Durable; each transport owns its queries."""
from abc import abstractmethod

from .agent_backend import Compacts, ConfiguresModel, ContextUsage, FailedCompaction, ReportsContext


class PiCapabilities(Compacts, ReportsContext, ConfiguresModel):
    @abstractmethod
    async def inspect(self, thread, command): ...

    def compaction_request(self, instructions):
        from .pi_commands import Compact

        return Compact(custom_instructions=instructions)

    async def compact(self, thread, instructions):
        from .native_pi import NativePiUnavailable

        try:
            return (await self.inspect(thread, self.compaction_request(instructions))).outcome
        except (NativePiUnavailable, ValueError) as error:
            return FailedCompaction(str(error))

    async def context_usage(self, thread):
        from .pi_commands import GetSessionStats

        usage = (await self.inspect(thread, GetSessionStats())).context_usage
        return (ContextUsage(None, None) if usage is None else
                ContextUsage(usage.tokens, usage.context_window))

    async def discover_models(self, thread):
        from .pi_commands import GetAvailableModels

        return await self.inspect(thread, GetAvailableModels())

    async def models(self, thread):
        data = await self.discover_models(thread)
        return tuple(dict.fromkeys(
            model.display_name for model in data.models if model.provider and model.id
        ))

    async def apply_settings(self, thread, commands):
        from .pi_commands import GetState

        for command in commands:
            await self.inspect(thread, command)
        return await self.inspect(thread, GetState())

    async def configure(self, thread, model, thinking):
        from .native_pi import NativePiUnavailable
        from .pi_commands import SetModel, SetThinkingLevel

        commands = []
        if model is not None:
            commands.append(SetModel.from_name(model))
        if thinking is not None:
            commands.append(SetThinkingLevel(level=thinking))
        if not commands:
            raise ValueError("A configuration change names a model or a thinking level")
        state = await self.apply_settings(thread, commands)
        selected, level = state.model.display_name, state.thinking_level.wire_value()
        if selected is None or level is None:
            raise NativePiUnavailable("Pi configuration did not report model and thinking level")
        return selected, level
