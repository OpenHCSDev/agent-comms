"""Narrow effects supplied by the remaining ACP turn/delivery owner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .runtime import RuntimeProxy
from .threads import Thread

if TYPE_CHECKING:
    from .cursor_publication import CursorPublication
    from .input_drain import InputDrain
    from .turn_runner import TurnRunner


class SessionEffects(ABC):
    cursors: CursorPublication
    inputs: InputDrain
    turns: TurnRunner

    @abstractmethod
    def _private_nk_marker(self) -> str: ...

    @abstractmethod
    def _create_runtime_proxy(self, thread: Thread, session_id: str) -> RuntimeProxy: ...

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...
