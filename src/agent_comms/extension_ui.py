"""One turn's extension UI admission, observed receipt and same-child replies."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .pi_events import CancelledUiChoice, DialogUiRequest, ExtensionUiChoice
from .pi_payloads import McpLiveReceipt
from .turn_failure import ExtensionUiFailed

if TYPE_CHECKING:
    from .backend import TurnSession


@dataclass
class ExtensionUiSession:
    controller: Callable[[DialogUiRequest], Awaitable[ExtensionUiChoice]] | None
    seen: set[str] = field(default_factory=set, init=False)
    receipt: McpLiveReceipt | None = field(default=None, init=False)

    def observe(self, text: str | None, session: TurnSession) -> McpLiveReceipt | None:
        # A receipt is informational, never permission to call a tool or send.
        if self.receipt is not None or not session.permits_live_receipt():
            return None
        self.receipt = McpLiveReceipt.from_status(text, session.original_input_id)
        return self.receipt

    async def choose(self, request: DialogUiRequest, session: TurnSession) -> ExtensionUiChoice:
        key = request.require_id()
        if not session.admission.permits_extension_ui(session):
            return CancelledUiChoice()
        if self.controller is None:
            return CancelledUiChoice()
        if key in self.seen or len(self.seen) >= 64:
            return CancelledUiChoice()
        self.seen.add(key)
        # The controller maps unanswered, disconnected and refused replies to
        # CancelledUiChoice itself; only this outer deadline is a denial here.
        with suppress(TimeoutError):
            return await asyncio.wait_for(self.controller(request), timeout=15)
        return CancelledUiChoice()

    async def fail(self, reason: str, session: TurnSession) -> None:
        session.output.record_failure(ExtensionUiFailed(reason))
        await session.native.proc.stop()
        session.finished = True

    async def answer(self, request: DialogUiRequest, session: TurnSession) -> None:
        try:
            choice = await self.choose(request, session)
        except ValueError as error:
            await self.fail(str(error), session)
            return
        # Input forwarding may become uncertain while the controller is awaiting
        # the user. Admission must still hold when its answer reaches Pi.
        if not session.admission.permits_extension_ui(session):
            choice = CancelledUiChoice()
        response = choice.response(request)
        try:
            await session.native.reply_ui(response)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            await self.fail(
                "Pi extension UI response could not reach the requesting child.",
                session,
            )
