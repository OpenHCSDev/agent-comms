"""The original turn and goal-account operations consumed while publishing progress."""

from typing import Protocol

from .agent_events import ProviderUsage, ToolEnd
from .registry.threads import Thread
from .routing import TurnRouting
from .transcripts import TranscriptCursor
from .turn_input_source import OriginalTurnInput
from .turn_lease import TurnLeaseFence
from .turn_terminal import TurnTerminal


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
