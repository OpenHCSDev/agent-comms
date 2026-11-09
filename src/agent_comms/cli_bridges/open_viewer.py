"""The long-lived sockets a connector shutdown must end, whatever route opened them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiohttp import web


@dataclass(frozen=True, eq=False, kw_only=True)
class OpenViewer:
    """One open socket a shutdown must end.

    Joining this is what a long-lived route owes: the connector's stop walks these and
    nothing else, so a socket that opens without joining holds the whole shutdown.
    """

    viewer: web.WebSocketResponse
    handler: asyncio.Task[None]
    """The request handler serving this socket."""

    def watching(self, thread: str) -> bool:
        """Whether this viewer is that thread's open inbox watch. Most sockets are nobody's."""
        return False

    async def ended(self) -> None:
        """Close the socket and end its handler. A handler busy away from the socket, as one
        still publishing its transcript is, does not see the close, and held the connector's
        stop for aiohttp's whole 60 s shutdown_timeout on 2026-11-06, while every watch's
        re-dial met 502."""
        await self.viewer.close()
        self.handler.cancel()


@dataclass(frozen=True, eq=False, kw_only=True)
class OpenObservation(OpenViewer):
    """A UI grant's open observation of the root. Nothing replaces it, and it ends the way
    every other long-lived socket here ends."""
