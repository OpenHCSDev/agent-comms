"""RPC correlation owns each response future and its original command."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from .sealed import Sealed

if TYPE_CHECKING:
    from .pi_commands import PiCommand
    from .pi_events import Response


@dataclass
class PendingRequest:
    future: asyncio.Future[Response]
    request: PiCommand

    def cancel(self) -> None:
        self.future.cancel()


class PendingRequests(Sealed):
    """One future per command family and request identity."""

    def __init__(self) -> None:
        self._pending: dict[tuple[type[PiCommand], str], PendingRequest] = {}

    def add(self, command: PiCommand) -> PendingRequest:
        key = (type(command), command.id or uuid4().hex)
        if key in self._pending:
            raise ValueError(f"Request already pending: {command.id}")
        pending = PendingRequest(asyncio.get_running_loop().create_future(), command)
        self._pending[key] = pending
        return pending

    def discard(self, result_type: type[PiCommand], request_id: str) -> None:
        pending = self._pending.pop((result_type, request_id), None)
        if pending is not None:
            pending.cancel()

    def take(self, result_type: type[PiCommand], request_id: str, value: Response) -> PiCommand | None:
        """Resolve a transport result and return its originating command, once."""
        pending = self._pending.pop((result_type, request_id), None)
        if pending is None:
            return None
        if not pending.future.done():
            pending.future.set_result(value)
        return pending.request

    def cancel_all(self) -> None:
        for pending in self._pending.values():
            pending.cancel()
        self._pending.clear()

    def take_anonymous(self, result_type: type[PiCommand], value: Response) -> PiCommand | None:
        for (owner, identity), pending in self._pending.items():
            if owner is result_type and pending.request.id is None:
                return self.take(owner, identity, value)
        return None
