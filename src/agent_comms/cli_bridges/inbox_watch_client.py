"""A cloud session's end of its inbox watch: run under Monitor with the URL comms_watch returned.

A cloud sandbox reaches the network only through its proxy, and Monitor's own WebSocket source
cannot use one, so this process holds the socket. Each pushed message reaches the model exactly as
it does on the bus host: a one-line notice naming a private file, because Monitor cuts a long line.
The session's transcript goes the other way on the same socket, so its work shows on the bus as a
local bridge's does. Every way either direction ends says so, since a silent end would look like
an empty inbox or an idle session.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from abc import ABC, abstractmethod
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar
from urllib.request import getproxies

from aiohttp import ClientError, ClientSession, ClientWebSocketResponse, WSMsgType

from agent_comms.bus import Bus
from agent_comms.bus_publication import Delivered
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.watched import FileSource

from .claude_transcript import WINDOW_ENDED, RecentClaudeWindow, RunningClaude
from .session_watch import WatchClaim
from .shared import IncomingFrame, MonitorReadFile


class TranscriptRoute(ABC):
    """How this session's transcript reaches its socket."""

    @abstractmethod
    async def sent(self, socket: ClientWebSocketResponse) -> None:
        """Send until the socket ends; a route with nothing to send says why."""


@dataclass(frozen=True)
class FollowedTranscript(RecentClaudeWindow, TranscriptRoute):
    """The session's own file, sent from its recent window onward as a local bridge reads it."""

    source: Path

    watch_url: ClassVar[re.Pattern[str]] = re.compile(r'wss?://[^\s"\'\\]+/watch/[\w-]+')
    hidden_url: ClassVar[str] = '<inbox watch URL>'

    @classmethod
    def line(cls, raw: bytes) -> str:
        """The transcript holds each watch URL this session was given, and the bus holds no tokens."""
        return cls.watch_url.sub(cls.hidden_url, raw.decode())

    async def lines_sent(self, socket: ClientWebSocketResponse, stream: BinaryIO, offset: int) -> int:
        """Every complete line from `offset`; where the next read starts."""
        for _, raw in self.complete_lines(stream, offset, stream.seek(0, 2)):
            await socket.send_str(self.line(raw))
            offset = stream.tell()
        return offset

    async def sent(self, socket: ClientWebSocketResponse) -> None:
        async with aclosing(Bus.watch_sources(FileSource(self.source))) as changes:
            await anext(changes)
            with self.source.open('rb') as stream:
                offset = self.history_start(stream, stream.seek(0, 2))
                offset = await self.lines_sent(socket, stream, offset)
                await socket.send_str(WINDOW_ENDED)
                while True:
                    await anext(changes)
                    offset = await self.lines_sent(socket, stream, offset)


@dataclass(frozen=True)
class UnfoundTranscript(TranscriptRoute):
    """Push still works without the transcript, so its absence is reported, not fatal."""

    reason: str

    async def sent(self, socket: ClientWebSocketResponse) -> None:
        print(f"Your work will not show on the bus: {self.reason}", flush=True)


def session_transcript() -> TranscriptRoute:
    try:
        return FollowedTranscript(RunningClaude(WatchClaim.environment_session()).source)
    except (ValueError, KeyError, OSError) as failure:
        return UnfoundTranscript(f"{type(failure).__name__}: {failure}")


@dataclass(frozen=True)
class InboxSocket:
    """One opened watch URL, where its messages are exported and what travels the other way."""

    url: str
    exports: Path
    transcript: TranscriptRoute = UnfoundTranscript("no transcript was given to this watch")

    def deliver(self, frame: str) -> None:
        message = FieldCodec.decode(Delivered, FieldCodec.parse(frame))
        notice = MonitorReadFile.naming(self.exports, message.reference)
        notice.export((IncomingFrame(message).text,))
        notice.emit()

    async def publishing(self, socket: ClientWebSocketResponse) -> None:
        try:
            await self.transcript.sent(socket)
        except (ClientError, OSError, ConnectionError) as failure:
            print(f"Your work stopped reaching the bus: {type(failure).__name__}: {failure}", flush=True)

    async def carried(self) -> str:
        """Deliver until the socket ends; what ended it, for the session to act on."""
        try:
            async with ClientSession() as session, session.ws_connect(
                self.url, proxy=getproxies().get("https"), heartbeat=25.0
            ) as socket:
                print("Watching your inbox; each message arrives as an event.", flush=True)
                publishing = asyncio.create_task(self.publishing(socket))
                try:
                    async for frame in socket:
                        if frame.type is WSMsgType.TEXT:
                            self.deliver(frame.data)
                finally:
                    publishing.cancel()
                return f"closed with code {socket.close_code}"
        except (ClientError, OSError) as failure:
            return f"failed: {type(failure).__name__}: {failure}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="The one-use URL comms_watch returned")
    exports = Path.home() / ".local" / "state" / "claude-comms-inbox" / "cloud" / "messages"
    socket = InboxSocket(parser.parse_args().url, exports, session_transcript())
    ended = asyncio.run(socket.carried())
    print(f"Your inbox watch {ended}. You are off the bus: call comms_watch and start this again.",
          flush=True)
    sys.exit(1)
