"""Turn and private-runtime effects consumed by the input owner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cursor_publication import CursorPublication
    from .turn_runner import TurnRunner


class InputEffects(ABC):
    cursors: CursorPublication
    turns: TurnRunner

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...

    @abstractmethod
    async def _drain_private_nk(self, session_id: str, wire_root_id: str) -> int: ...

    @abstractmethod
    def _private_nk_marker(self) -> str: ...

    @abstractmethod
    def _configured_root_id(self) -> str: ...

    _private_nk_native_package: Path | None
