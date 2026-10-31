"""Typed request correlation shared by internal setting results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .nominal.sealed import Sealed
from .turn_item import PiRequestKey

if TYPE_CHECKING:
    from .pi_commands import PiCommand
    from .pi_events import Response

@dataclass(frozen=True)
class PendingRequest[Command: PiCommand]:
    future: asyncio.Future[Response]
    request: Command

    async def response(self) -> Response:
        """A consumer may stop waiting without cancelling the native reply."""
        return await asyncio.shield(self.future)


class PendingRequests(Sealed):
    """One future per nominal result family and request identity."""

    def __init__(self) -> None:
        self._pending: dict[tuple[type[PiCommand], PiRequestKey], PendingRequest[PiCommand]] = {}

    def add[Command: PiCommand](
        self, result_type: type[PiCommand], request_id: PiRequestKey, *, request: Command
    ) -> PendingRequest[Command]:
        key = (result_type, request_id)
        if key in self._pending:
            raise ValueError(f"Request already pending: {request_id}")
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        pending = PendingRequest(future, request)
        self._pending[key] = pending
        return pending

    def take(
        self, result_type: type[PiCommand], request_id: PiRequestKey, value: Response,
    ) -> PiCommand | None:
        """Resolve a transport result and return its originating command, once."""
        pending = self._pending.pop((result_type, request_id), None)
        if pending is None:
            return None
        if not pending.future.done():
            pending.future.set_result(value)
        return pending.request

    async def before_settlement(self) -> None:
        """Join written commands whose declared preflight can precede native work."""
        await asyncio.gather(*(
            pending.request.before_settlement(pending)
            for pending in tuple(self._pending.values())
        ))

    def cancel_all(self) -> None:
        for pending in self._pending.values():
            pending.future.cancel()
        self._pending.clear()

    def take_anonymous(self, result_type: type[PiCommand], value: Response) -> PiCommand | None:
        for (owner, identity), pending in self._pending.items():
            if owner is result_type and not pending.request.id.reported:
                return self.take(owner, identity, value)
        return None
