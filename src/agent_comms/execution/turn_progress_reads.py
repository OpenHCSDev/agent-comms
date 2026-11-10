"""The original turn and goal-account operations consumed while publishing progress."""

from abc import ABC, abstractmethod

from agent_comms.agent_events import ProviderUsage, ToolEnd
from agent_comms.execution.turn_input_source import OriginalTurnInput
from agent_comms.execution.turn_lease import TurnLeaseFence
from agent_comms.execution.turn_terminal import TurnTerminal
from agent_comms.messaging.routing import TurnRouting
from agent_comms.registry.threads import Thread
from agent_comms.execution.transcript_sources import TranscriptCursor


class ProgressTurn(ABC):
    __slots__ = ()

    terminal: TurnTerminal
    routing: TurnRouting
    original: OriginalTurnInput
    checkpoint: TranscriptCursor

    @property
    @abstractmethod
    def thread(self) -> Thread: ...

    @property
    @abstractmethod
    def turn_lease(self) -> TurnLeaseFence: ...

    @property
    @abstractmethod
    def reply_targets(self) -> tuple[str, ...]: ...

    @abstractmethod
    async def attach_native_session(self, session_file: str) -> None: ...


class ProgressGoalAccount(ABC):
    __slots__ = ()

    has_productive_tool: bool

    @abstractmethod
    def provider_usage(self, event: ProviderUsage) -> None: ...

    @abstractmethod
    def tool_ended(self, event: ToolEnd) -> None: ...
