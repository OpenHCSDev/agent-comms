"""Narrow effects supplied by the remaining ACP turn/delivery owner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .nominal.field_codec import AcpSessionId
from .registry.threads import Thread
from .runtime import RuntimeProxy

if TYPE_CHECKING:
    from .cursor_publication import CursorPublication
    from .execution.input_drain import InputDrain
    from .execution.turn_runner import TurnRunner


class SessionEffects(ABC):
    cursors: CursorPublication
    inputs: InputDrain
    turns: TurnRunner

    @abstractmethod
    def _create_runtime_proxy(self, thread: Thread, session_id: AcpSessionId) -> RuntimeProxy: ...

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...
