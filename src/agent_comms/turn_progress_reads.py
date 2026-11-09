"""The original turn and goal-account operations consumed while publishing progress."""

from typing import Protocol

from agent_comms.agent_events import ProviderUsage
from agent_comms.agent_events import ToolEnd
from agent_comms.registry.threads import Thread
from agent_comms.routing import TurnRouting
from agent_comms.transcripts import TranscriptCursor
from agent_comms.execution.turn_input_source import OriginalTurnInput
from agent_comms.execution.turn_lease import TurnLeaseFence
from agent_comms.execution.turn_terminal import TurnTerminal


class ProgressTurn(Protocol):
    terminal: TurnTerminal

    @property
    def thread(self) -> Thread: ...

    @property
    def turn_lease(self) -> TurnLeaseFence: ...

    @property
    def routing(self) -> TurnRouting: ...

    @property
    def original(self) -> OriginalTurnInput: ...

    @property
    def checkpoint(self) -> TranscriptCursor: ...

    @property
    def reply_targets(self) -> tuple[str, ...]: ...

    async def attach_native_session(self, session_file: str) -> None: ...


class ProgressGoalAccount(Protocol):
    @property
    def has_productive_tool(self) -> bool: ...

    def provider_usage(self, event: ProviderUsage) -> None: ...

    def tool_ended(self, event: ToolEnd) -> None: ...
