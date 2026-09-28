"""Turn and private-runtime effects consumed by the input owner."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .declarations import Message


class InputEffects(ABC):
    @abstractmethod
    def _debug_log(self, message: str) -> None: ...

    @abstractmethod
    async def _drain_private_nk(self, session_id: str, wire_root_id: str) -> int: ...

    @abstractmethod
    def _private_nk_marker(self) -> str | None: ...

    _private_nk_native_package: Path | None
    _agent_bin: str
    _agent_args: list[str]

    @abstractmethod
    async def _run_agent_turn(
        self,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        reply_targets: tuple[str, ...] = (),
        origins: tuple[Message, ...] = (),
        images: tuple[Any, ...] = (),
        original_keys: tuple[str, ...] = (),
        initial_display_text: str | None = None,
        autonomous_goal: bool = False,
        original_owner_input: bool = False,
        original_goal_id: str | None = None,
        dependency_wait_id: str | None = None,
        direct_interrupt_goal_id: str | None = None,
        direct_interrupt_goal_revision: int | None = None,
        direct_interrupt_wait_id: str | None = None,
        direct_interrupt_input_key: str | None = None,
        direct_interrupt_ticket: str | None = None,
    ) -> None: ...

    @abstractmethod
    def _schedule_goal(self, session_id: str) -> None: ...

    @abstractmethod
    async def _sync_goal_execution(self, session_id: str, thread_name: str) -> None: ...

    _turn_locks: dict[str, asyncio.Lock]
    _turn_tasks: dict[str, asyncio.Task[Any]]
