"""Typed request correlation shared by internal setting results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_events import SettingChangeResult


@dataclass
class PendingRequest:
    future: asyncio.Future[Any]
    request: Any = None


class PendingRequests:
    """One future per nominal result family and request identity."""

    def __init__(self) -> None:
        self._pending: dict[tuple[type, str], PendingRequest] = {}

    def add(
        self, result_type: type, request_id: str, *, request: Any = None
    ) -> asyncio.Future[Any]:
        key = (result_type, request_id)
        if key in self._pending:
            raise ValueError(f"Request already pending: {request_id}")
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = PendingRequest(future, request)
        return future

    def discard(self, result_type: type, request_id: str) -> None:
        self._pending.pop((result_type, request_id), None)

    def resolve(self, result: SettingChangeResult) -> None:
        for owner in type(result).__mro__:
            pending = self._pending.get((owner, result.id))
            if pending is not None:
                future = pending.future
                if not future.done():
                    if result.ok:
                        future.set_result(None)
                    else:
                        future.set_exception(RuntimeError(str(result.error)))
                return

    def take(self, result_type: type, request_id: str, value: Any) -> Any:
        """Resolve a transport result and return its originating command, once."""
        pending = self._pending.pop((result_type, request_id), None)
        if pending is None:
            return None
        if not pending.future.done():
            pending.future.set_result(value)
        return pending.request

    def cancel_all(self) -> None:
        for pending in self._pending.values():
            pending.future.cancel()
        self._pending.clear()
