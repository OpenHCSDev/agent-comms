"""Typed request correlation shared by internal setting results."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_events import SettingChangeResult


class PendingRequests:
    """One future per nominal result family and request identity."""

    def __init__(self) -> None:
        self._pending: dict[tuple[type, str], asyncio.Future[Any]] = {}

    def add(self, result_type: type, request_id: str) -> asyncio.Future[Any]:
        key = (result_type, request_id)
        if key in self._pending:
            raise ValueError(f"Request already pending: {request_id}")
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        return future

    def discard(self, result_type: type, request_id: str) -> None:
        self._pending.pop((result_type, request_id), None)

    def resolve(self, result: SettingChangeResult) -> None:
        for owner in type(result).__mro__:
            future = self._pending.get((owner, result.id))
            if future is not None:
                if not future.done():
                    if result.ok:
                        future.set_result(None)
                    else:
                        future.set_exception(RuntimeError(str(result.error)))
                return
