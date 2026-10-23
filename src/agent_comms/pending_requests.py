"""Typed request correlation shared by internal setting results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .sealed import Sealed

if TYPE_CHECKING:
    from .pi_commands import PiCommand
    from .pi_events import Response

@dataclass
class PendingRequest:
    future: asyncio.Future[Response]
    request: PiCommand


class PendingRequests(Sealed):
    """One future per nominal result family and request identity."""

    def __init__(self) -> None:
        self._pending: dict[tuple[type[PiCommand], str], PendingRequest] = {}

    def add(
        self, result_type: type[PiCommand], request_id: str, *, request: PiCommand
    ) -> asyncio.Future[Response]:
        key = (result_type, request_id)
        if key in self._pending:
            raise ValueError(f"Request already pending: {request_id}")
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[key] = PendingRequest(future, request)
        return future

    def discard(self, result_type: type[PiCommand], request_id: str) -> None:
        self._pending.pop((result_type, request_id), None)

    def take(
        self, result_type: type[PiCommand], request_id: str, value: Response,
    ) -> PiCommand | None:
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

    def take_anonymous(self, result_type: type[PiCommand], value: Response) -> PiCommand | None:
        for (owner, identity), pending in self._pending.items():
            if owner is result_type and not pending.request.id.reported:
                return self.take(owner, identity, value)
        return None
