"""Narrow effects supplied by the remaining ACP turn/delivery owner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agent_comms.execution.runtime import RuntimeProxy
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.registry.threads import Thread

if TYPE_CHECKING:
    from agent_comms.execution.cursor_publication import CursorPublication
    from agent_comms.execution.input_drain import InputDrain
    from agent_comms.execution.turn_runner import TurnRunner


class SessionEffects(ABC):
    cursors: CursorPublication
    inputs: InputDrain
    turns: TurnRunner

    @abstractmethod
    def _create_runtime_proxy(self, thread: Thread, session_id: AcpSessionId) -> RuntimeProxy: ...

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...
