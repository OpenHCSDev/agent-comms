from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from typing import TYPE_CHECKING

from acp.schema import PromptRequest

if TYPE_CHECKING:
    from agent_comms.execution.session_lifecycle import SessionLifecycle
    from agent_comms.execution.turn_runner import TurnRunner
    from agent_comms.transport.runtime_server import RuntimeServer


class TurnEffects(ABC):
    _runtime: RuntimeServer
    sessions: SessionLifecycle
    turns: TurnRunner

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...

    @staticmethod
    @abstractmethod
    def _prompt_text(request: PromptRequest) -> str: ...
