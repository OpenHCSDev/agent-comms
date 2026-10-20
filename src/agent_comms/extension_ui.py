"""One turn's extension UI permission, observed input record and same-child replies."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .pi_events import CancelledUiChoice, DialogUiRequest, ExtensionUiChoice
from .pi_payloads import McpLiveAcknowledgement
from .turn_failure import ExtensionUiFailed

if TYPE_CHECKING:
    from .backend import TurnSession


@dataclass
class ExtensionUiSession:
    controller: Callable[[DialogUiRequest], Awaitable[ExtensionUiChoice]] | None
    seen: set[str] = field(default_factory=set, init=False)
    acknowledgement: McpLiveAcknowledgement | None = field(default=None, init=False)

    def observe(self, text: str | None, session: TurnSession) -> McpLiveAcknowledgement | None:
        # A acknowledgement is informational, never permission to call a tool or send.
        if self.acknowledgement is not None or not session.permits_live_status():
            return None
        self.acknowledgement = McpLiveAcknowledgement.from_status(text, session.command.input_id)
        return self.acknowledgement

    async def choose(self, request: DialogUiRequest, session: TurnSession) -> ExtensionUiChoice:
        key = request.require_id()
        if not session.prompt_state.permits_extension_ui(session):
            return CancelledUiChoice()
        if self.controller is None:
            return CancelledUiChoice()
        if key in self.seen or len(self.seen) >= 64:
            return CancelledUiChoice()
        self.seen.add(key)
        # The controller maps unanswered, disconnected and refused replies to
        # CancelledUiChoice itself; only this outer deadline is a denial here.
        deadline = asyncio.timeout(15)
        try:
            async with deadline:
                return await self.controller(request)
        except TimeoutError:
            if not deadline.expired():
                raise
            return CancelledUiChoice()

    async def fail(self, reason: str, session: TurnSession) -> None:
        session.output.record_failure(ExtensionUiFailed(reason))
        await session.native.shutdown()
        session.finish_request()

    async def answer(self, request: DialogUiRequest, session: TurnSession) -> None:
        choice = await self.choose(request, session)
        # Input forwarding may become uncertain while the controller is awaiting
        # the user. Input permission must still hold when its answer reaches Pi.
        if not session.prompt_state.permits_extension_ui(session):
            choice = CancelledUiChoice()
        response = choice.response(request)
        try:
            await session.native.reply_ui(response)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            await self.fail(
                "Pi extension UI response could not reach the requesting child.",
                session,
            )
