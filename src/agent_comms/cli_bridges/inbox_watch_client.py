"""A cloud session's end of its inbox watch: run under Monitor with the URL comms_watch returned.

A cloud sandbox reaches the network only through its proxy, and Monitor's own WebSocket source
cannot use one, so this process holds the socket. Each pushed message reaches the model as written
when it fits one Monitor event, otherwise as a one-line notice naming a private file, because
Monitor cuts a long line.
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

from aiohttp import ClientError, ClientSession, ClientWebSocketResponse, WSMsgType
from aiohttp.helpers import get_env_proxy_for_url
from yarl import URL

from agent_comms.bus import Bus
from agent_comms.bus_publication import Delivered
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.watched import FileSource

from .claude_protocol import ClaudeMonitorFrame
from .claude_transcript import WATCH_STOPPED, WINDOW_ENDED, RecentClaudeWindow, RunningClaude
from .session_watch import WatchClaim


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
    hidden_url: ClassVar[str] = '[inbox watch URL]'
    """It lands inside whatever held the URL, including a task notification's XML, so it escapes
    nothing and needs no escaping."""

    @classmethod
    def line(cls, raw: bytes) -> str:
        """The transcript holds each watch URL this session was given, and the bus holds no tokens."""
        return cls.hidden(raw.decode())

    @classmethod
    def hidden(cls, text: str) -> str:
        return cls.watch_url.sub(cls.hidden_url, text)

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


class WatchedMonitorFrame(ClaudeMonitorFrame):
    """No transcript decode confirms a watched delivery: the server records it when it sends the
    frame. So a message that fits one event is shown exactly as written."""

    def shown_inline(self) -> str:
        return self.inline

    @property
    def sender(self) -> str:
        """No decoder reads this header, so it also says what kind of sender this is."""
        return self.message.sender_label

    @property
    def identity(self) -> str:
        """No decoder reads this header, so it names the whole reference a reply must cite."""
        return self.message.reference.name

    @property
    def shows_safely_inline(self) -> bool:
        """Shown as written, each body line is a line of the event, and the transcript decoder
        reads every line: a body quoting one of our frames goes to the file, where nothing
        decodes it."""
        return not any(self.claims(line) for line in self.message.body.split('\n'))


class WatchEnd(ABC):
    """How a watch ended, and what the session does about it."""

    exit_code: ClassVar[int]

    @abstractmethod
    def line(self) -> str: ...

    @classmethod
    def closed(cls, code: int | None) -> WatchEnd:
        """The close code is decided once, here, where it arrives."""
        return WatchStopped() if code == WATCH_STOPPED else WatchDropped(f"closed with code {code}")


@dataclass(frozen=True)
class WatchDropped(WatchEnd):
    """An install, a network drop or a newer watch: the session is still wanted on the bus."""

    reason: str
    exit_code = 1

    def line(self) -> str:
        return (f"Your inbox watch {self.reason}. You are off the bus: call comms_watch and start "
                "this again.")


@dataclass(frozen=True)
class WatchStopped(WatchEnd):
    """The thread was stopped on the bus: re-arming would undo the stop."""

    exit_code = 0

    def line(self) -> str:
        return ("This thread was stopped on the bus. Do not call comms_watch again: tell the "
                "person in one line that you were stopped, then stop.")


@dataclass(frozen=True)
class InboxSocket:
    """One opened watch URL, where its messages are exported and what travels the other way."""

    url: str
    exports: Path
    transcript: TranscriptRoute = UnfoundTranscript("no transcript was given to this watch")

    batch_gap_seconds: ClassVar[float] = 0.25
    """Monitor joins the lines of each 200 ms into one event and cuts a long event mid-line: of 11
    notices at once, the last never reached the model though the bus recorded them as taken. A
    local bridge holds such a burst in its window until the transcript shows each one seen; this
    side has no such confirmation, so each message gets a batch of its own."""

    async def deliver(self, frame: str) -> None:
        """Inline when it fits one Monitor event, otherwise one notice naming its whole body."""
        _, line = WatchedMonitorFrame(FieldCodec.decode(Delivered, FieldCodec.parse(frame))).event(
            self.exports)
        print(line, flush=True)
        await asyncio.sleep(self.batch_gap_seconds)

    async def publishing(self, socket: ClientWebSocketResponse) -> None:
        try:
            await self.transcript.sent(socket)
        except (ClientError, OSError, ConnectionError) as failure:
            print(f"Your work stopped reaching the bus: {type(failure).__name__}: {failure}", flush=True)

    @property
    def proxy(self) -> URL | None:
        """A wss socket rides the environment's https proxy, and the environment's bypass list
        (NO_PROXY, loopback) still holds; aiohttp alone looks only for a wss proxy."""
        try:
            return get_env_proxy_for_url(URL(self.url).with_scheme("https"))[0]
        except LookupError:
            return None

    async def carried(self) -> WatchEnd:
        """Deliver until the socket ends; what ended it, for the session to act on."""
        try:
            async with ClientSession() as session, session.ws_connect(
                self.url, proxy=self.proxy, heartbeat=25.0
            ) as socket:
                print("Watching your inbox; each message arrives as an event.", flush=True)
                publishing = asyncio.create_task(self.publishing(socket))
                try:
                    async for frame in socket:
                        if frame.type is WSMsgType.TEXT:
                            await self.deliver(frame.data)
                finally:
                    publishing.cancel()
                return WatchEnd.closed(socket.close_code)
        except (ClientError, OSError) as failure:
            return WatchDropped(
                f"failed: {type(failure).__name__}: {FollowedTranscript.hidden(str(failure))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="The one-use URL comms_watch returned")
    exports = Path.home() / ".local" / "state" / "claude-comms-inbox" / "cloud" / "messages"
    socket = InboxSocket(parser.parse_args().url, exports, session_transcript())
    ended = asyncio.run(socket.carried())
    print(ended.line(), flush=True)
    sys.exit(ended.exit_code)
