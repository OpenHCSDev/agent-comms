"""Narrow effects supplied by the remaining ACP turn/delivery owner."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from .declarations import Thread
from .runtime import RuntimeProxy


class SessionEffects(ABC):
    @abstractmethod
    def _private_session_mode(self) -> bool: ...

    @abstractmethod
    def _initialize_session_delivery(
        self, session_id: str, thread: Thread, *, fresh: bool, private: bool
    ) -> None: ...

    @abstractmethod
    def _ensure_live_drain(self, session_id: str) -> None: ...

    @abstractmethod
    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None: ...

    @abstractmethod
    def _session_runtime_metadata(self, thread_name: str, session_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def _create_runtime_proxy(self, thread: Thread, session_id: str) -> RuntimeProxy: ...

    @abstractmethod
    async def _close_idle_backend(self, session_id: str) -> None: ...

    @abstractmethod
    def _active_backend_inbox(
        self, session_id: str
    ) -> asyncio.Queue[str | dict[str, Any]] | None: ...

    @abstractmethod
    async def _sync_goal_execution(self, session_id: str, thread_name: str) -> None: ...

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...
