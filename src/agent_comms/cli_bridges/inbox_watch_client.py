"""A cloud session's end of its inbox watch: run under Monitor with the URL comms_watch returned.

A cloud sandbox reaches the network only through its proxy, and Monitor's own WebSocket source
cannot use one, so this process holds the socket. Each pushed message reaches the model as written,
across the lines of one Monitor batch, when the batch holds it; otherwise as a one-line notice
naming a private file, because Monitor cuts a long batch.
The session's transcript goes the other way on the same socket, so its work shows on the bus as a
local bridge's does. Every way either direction ends says so, since a silent end would look like
an empty inbox or an idle session.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
import time
from abc import ABC, abstractmethod
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
from typing import BinaryIO, ClassVar

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    WSMsgType,
    WSServerHandshakeError,
)
from aiohttp.helpers import get_env_proxy_for_url
from yarl import URL

from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.claude_transcript import (
    WATCH_DEFERRED,
    WATCH_REPLACED,
    WATCH_STOPPED,
    WINDOW_ENDED,
    PrintedLedger,
    PrintedNow,
    RecentClaudeWindow,
    ReconnectsAlone,
    ReconnectUrl,
    RunningClaude,
)
from agent_comms.cli_bridges.harness_delivery_states import PrintLedgerId
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.execution.launch_runtime import RunningSource
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.watched import FileSource
from agent_comms.nominal.field_codec import FieldCodec


class PrintLedger:
    """A session with no transcript is no session this machine can name: it keeps no ledger
    and sends none, so a connector never replays anything to it."""

    def record(self, reference: MessageReference) -> None:
        """Nothing is kept, so nothing can be proved."""

    def reports(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class SessionLedger(PrintLedger):
    """What one session's watches may have printed, one reference per line. It is the session's
    own, so a channel message printed in one session proves nothing about another's attempt."""

    path: Path

    def record(self, reference: MessageReference) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as stream:
            stream.write(f"{reference.name}\n")
            stream.flush()
            os.fsync(stream.fileno())

    @property
    def identity(self) -> PrintLedgerId:
        """Born with the ledger and kept beside it: a fresh container's ledger is a new one."""
        path = self.path.with_suffix(".id")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x") as stream:
                stream.write(secrets.token_hex(16))
        except FileExistsError:
            pass
        return PrintLedgerId(path.read_text())

    def reports(self) -> tuple[str, ...]:
        try:
            names = self.path.read_text().split()
        except FileNotFoundError:
            names = []
        return (PrintedLedger(ledger=self.identity,
                              printed=tuple(map(MessageReference.named, names))).line(),)


class TranscriptRoute(ABC):
    """How this session's transcript reaches its socket."""

    @abstractmethod
    async def sent(self, socket: ClientWebSocketResponse) -> None:
        """Send until the socket ends; a route with nothing to send says why."""

    def ledger(self, state: Path) -> PrintLedger:
        return PrintLedger()


@dataclass(frozen=True)
class FollowedTranscript(RecentClaudeWindow, TranscriptRoute):
    """The session's own file, sent from its recent window onward as a local bridge reads it."""

    source: Path

    def ledger(self, state: Path) -> PrintLedger:
        return SessionLedger(state / "printed" / self.source.stem)

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


class WatchEnd(ABC):
    """How a watch ended, and what the session does about it."""

    exit_code: ClassVar[int]

    @abstractmethod
    def line(self) -> str: ...

    @classmethod
    def closed(cls, code: int | None, said: str) -> WatchEnd:
        """The close code is decided once, here, where it arrives; ``said`` is the connector's
        own reason, when its close frame gave one."""
        reason = f"closed with code {code}{said}"
        match code:
            case int(stopped) if stopped == WATCH_STOPPED:
                return WatchStopped()
            case int(replaced) if replaced == WATCH_REPLACED:
                return WatchReplaced(reason)
            case int(deferred) if deferred == WATCH_DEFERRED:
                return WatchDeferred(reason)
            case _:
                return WatchDropped(reason)

    async def settled(self, watch: InboxSocket) -> WatchEnd:
        """What is left for the model once this process has done what it can."""
        return self

    async def redialing(self, watch: InboxSocket, url: str, pauses: Pauses) -> WatchEnd:
        """How a reconnect attempt that ended this way goes on: by default it is over."""
        return self


@dataclass(frozen=True)
class WatchDropped(WatchEnd):
    """An install, a network drop or a newer watch: the session is still wanted on the bus."""

    reason: str
    exit_code = 1

    def line(self) -> str:
        return (f"Your inbox watch {self.reason}. You are off the bus: call comms_watch and start "
                "this again.")

    async def settled(self, watch: InboxSocket) -> WatchEnd:
        """A socket that opened and then dropped re-dials with the reconnect URL it was given."""
        return await watch.redials.held.redialed(watch, self)

    async def redialing(self, watch: InboxSocket, url: str, pauses: Pauses) -> WatchEnd:
        """The reconnect opened and later dropped too: it re-dials with the newer URL it holds."""
        return await self.settled(watch)


@dataclass(frozen=True)
class WatchUnopened(WatchDropped):
    """The socket never opened: the path failed before the connector saw the ticket, so the
    same URL is still good and is tried again after a pause."""

    async def settled(self, watch: InboxSocket) -> WatchEnd:
        return self

    async def redialing(self, watch: InboxSocket, url: str, pauses: Pauses) -> WatchEnd:
        return await pauses.retried(watch, url, self)


@dataclass(frozen=True)
class WatchUnreachable(WatchUnopened):
    """Nothing answered behind the proxy (502 to 504), as while the connector restarts. Every
    install restarts it, and on 2026-11-06 a re-dial after the restart's close met 502s past the
    unopened run's 15 s and gave up. The URL never reached the connector, so it is still good,
    and it is dialled again every few seconds for as long as nothing is listening."""

    async def redialing(self, watch: InboxSocket, url: str, pauses: Pauses) -> WatchEnd:
        return await watch.redials.restarting.retried(watch, url, self)


@dataclass(frozen=True)
class WatchReplaced(WatchDropped):
    """A newer watch of this thread took the socket's place. Reconnecting would take it back
    and the two would replace each other for ever, so this one goes back to the model."""

    async def settled(self, watch: InboxSocket) -> WatchEnd:
        return self


@dataclass(frozen=True)
class WatchRefused(WatchDropped):
    """The connector refused the URL: spent, superseded, or forgotten by a restart. Only the
    model can get another."""

    async def settled(self, watch: InboxSocket) -> WatchEnd:
        return self


@dataclass(frozen=True)
class WatchDeferred(WatchDropped):
    """The bus is under maintenance, as every install holds it. The socket opened and was handed
    its next URL before the connector found it could not attach the thread, so that URL waits
    out the maintenance; it spends no reset, since nothing reset. On 2026-11-06 a watch re-dialled
    into an install five times in seconds, each one refused, and gave up before it ended. A
    deferral is the connector, alive, saying the maintenance still holds, so the wait has no
    bound while that is the answer: that install held it for 5 min 16 s."""

    async def settled(self, watch: InboxSocket) -> WatchEnd:
        return await watch.redials.held.waiting(watch, self)

    async def redialing(self, watch: InboxSocket, url: str, pauses: Pauses) -> WatchEnd:
        """Still under maintenance: the newest URL waits on, whatever pauses were left."""
        return await watch.redials.held.waited(watch, self, watch.redials.still_held)


@dataclass(frozen=True)
class WatchStopped(WatchEnd):
    """The thread was stopped on the bus: re-arming now would undo the stop. The session is
    not archived; it connects again when someone wakes it."""

    exit_code = 0

    def line(self) -> str:
        return ("This thread was stopped on the bus. Do not call comms_watch again now: tell the "
                "person in one line that you were stopped, then stop. This session stays; when "
                "someone wakes it, connect again.")


@dataclass(frozen=True)
class RuntimeReplaced(WatchEnd):
    """Core was reinstalled while this watch ran from the runtime it replaced. Re-dialling would
    keep the old client watching for ever: on 2026-11-06 the host's watch survived three installs
    on its first runtime, missed every client fix, and showed up as an unretired old-runtime PID
    each time. Only a watch started anew runs the new runtime, so this one goes back to the model."""

    exit_code = 1

    def line(self) -> str:
        return ("Core was reinstalled, and this watch was running the old runtime, so it has "
                "stopped. Call comms_watch and start this again to watch on the new one.")


WATCHING = "Watching your inbox; each message arrives as an event."
RECONNECTS_ALONE = "This watch reconnects on its own after a network reset."
"""Said when the connector hands over a reconnect URL; the URL itself is never shown."""
WAITS_FOR_MAINTENANCE = ("The bus is under maintenance, as during an install; this watch waits for "
                         "it and reconnects on its own.")
"""Said once, when a maintenance wait begins."""


class Announcement:
    """What this process has told the session about its watch. Every printed line wakes the
    session for a turn, so each fact is said once per process: a reconnect that works is the
    watch doing its job and says nothing; one that fails ends with its own line. On 2026-11-06
    each reset printed three lines, and teammates spent a dozen turns acknowledging them."""

    def opened(self) -> Announcement:
        return self

    def reconnectable(self) -> Announcement:
        return self


class Unannounced(Announcement):
    """Nothing said yet: the first socket to open says the watch is up."""

    def opened(self) -> Announcement:
        print(WATCHING, flush=True)
        return Watching()


class Watching(Announcement):
    """Said to be watching: the first reconnect URL says the watch outlives a reset."""

    def reconnectable(self) -> Announcement:
        print(RECONNECTS_ALONE, flush=True)
        return Announcement()


class Pauses(ABC):
    """The pauses left before the same unopened URL is tried again."""

    @abstractmethod
    async def retried(self, watch: InboxSocket, url: str, ended: WatchDropped) -> WatchEnd: ...

    @classmethod
    def of(cls, delays: tuple[float, ...]) -> Pauses:
        last: Pauses = NoPauses()
        return reduce(lambda rest, delay: Pause(delay, rest), reversed(delays), last)


class NoPauses(Pauses):
    """None left: the URL never opened, and that is what the model hears."""

    async def retried(self, watch: InboxSocket, url: str, ended: WatchDropped) -> WatchEnd:
        return ended


@dataclass(frozen=True)
class Pause(Pauses):
    """Wait, then dial the same URL; whatever that ends as goes on with the pauses after it."""

    delay: float
    rest: Pauses

    async def retried(self, watch: InboxSocket, url: str, ended: WatchDropped) -> WatchEnd:
        await asyncio.sleep(self.delay)
        return await (await watch.opened(url)).redialing(watch, url, self.rest)


class HeldReconnect(ABC):
    """What this process re-dials with after a reset. It is a bearer credential, so it lives in
    this process's memory only: never printed, never in the ledger, never in a failure line."""

    @abstractmethod
    async def redialed(self, watch: InboxSocket, dropped: WatchDropped) -> WatchEnd: ...

    @abstractmethod
    async def waited(self, watch: InboxSocket, deferred: WatchDeferred, pauses: Pauses) -> WatchEnd: ...

    async def waiting(self, watch: InboxSocket, deferred: WatchDeferred) -> WatchEnd:
        """The first refusal of a maintenance, which may be about to end."""
        return await self.waited(watch, deferred, watch.redials.first_wait)


class NoReconnect(HeldReconnect):
    """The connector gave this socket no reconnect URL: an older connector, or it is spent."""

    async def redialed(self, watch: InboxSocket, dropped: WatchDropped) -> WatchEnd:
        return dropped

    async def waited(self, watch: InboxSocket, deferred: WatchDeferred, pauses: Pauses) -> WatchEnd:
        return deferred


@dataclass(frozen=True)
class ReconnectWith(HeldReconnect):
    """The standing reconnect URL from the connector's last ReconnectUrl frame."""

    url: str

    async def redialed(self, watch: InboxSocket, dropped: WatchDropped) -> WatchEnd:
        watch.redials.held = NoReconnect()
        if not watch.redials.has_room(time.monotonic()):
            print(f"Your inbox watch {dropped.reason}, and it has reset "
                  f"{watch.redials.most} times in {watch.redials.window_seconds} s; giving the "
                  "reconnect back to you.", flush=True)
            return dropped
        return await (await watch.opened(self.url)).redialing(
            watch, self.url, watch.redials.pauses)

    async def waiting(self, watch: InboxSocket, deferred: WatchDeferred) -> WatchEnd:
        """Said once, when the wait begins: every printed line wakes the session."""
        print(WAITS_FOR_MAINTENANCE, flush=True)
        return await super().waiting(watch, deferred)

    async def waited(self, watch: InboxSocket, deferred: WatchDeferred, pauses: Pauses) -> WatchEnd:
        """Dial this URL after the next pause; the socket it opens spends it and hands over the
        next one, which is what a further wait dials."""
        watch.redials.held = NoReconnect()
        return await pauses.retried(watch, self.url, deferred)


@dataclass
class Redials:
    """This process's reconnects: the URL it holds and when it last used one. A path that resets
    every few seconds must not spin, so reconnects are bounded by count within a window of wall
    time, and the retries of one reconnect by a fixed run of pauses."""

    held: HeldReconnect = field(default_factory=NoReconnect)
    used: list[float] = field(default_factory=list)
    told: Announcement = field(default_factory=Unannounced)

    most: ClassVar[int] = 5
    window_seconds: ClassVar[int] = 600
    pauses: ClassVar[Pauses] = Pauses.of((1.0, 2.0, 4.0, 8.0))
    first_wait: ClassVar[Pauses] = Pause(5.0, pauses)
    still_held: ClassVar[Pauses] = Pause(30.0, pauses)
    """Before each re-dial into a maintenance that still holds. A dial that does not open at all
    has the ordinary pauses after it, then the model."""
    restarting: ClassVar[Pauses] = Pause(5.0, pauses)
    """Before each re-dial while nothing is listening behind the proxy."""

    def has_room(self, now: float) -> bool:
        """Whether the window has room for one more reconnect, which is then recorded."""
        self.used = [at for at in self.used if now - at < self.window_seconds]
        if len(self.used) >= self.most:
            return False
        self.used.append(now)
        return True


@dataclass(frozen=True)
class SocketFailure:
    """A network failure on the watch socket, decoded once where it was caught."""

    error: ClientError | OSError

    @property
    def reason(self) -> str:
        return f"failed: {type(self.error).__name__}: {FollowedTranscript.hidden(str(self.error))}"

    @property
    def dialled(self) -> type[WatchDropped]:
        """What a failure before the socket opened means, decided once from the handshake: the
        connector refused the URL itself (403: spent, superseded or forgotten); nothing was
        listening behind the proxy (502 to 504, as while it restarts); or the path failed some
        other way before the connector saw the ticket."""
        match self.error:
            case WSServerHandshakeError(status=403):
                return WatchRefused
            case WSServerHandshakeError(status=502 | 503 | 504):
                return WatchUnreachable
            case _:
                return WatchUnopened


class SocketStage(ABC):
    """Where a socket's life was when a network failure struck it."""

    @abstractmethod
    def ended(self, failure: SocketFailure) -> WatchDropped: ...


class Dialing(SocketStage):
    """Before the socket opened: a refusal of the URL is final; anything else struck the path
    before the connector saw the ticket, so the same URL may still open."""

    def ended(self, failure: SocketFailure) -> WatchDropped:
        return failure.dialled(failure.reason)


class Open(SocketStage):
    """After it opened: a write after the path reset ('Cannot write to closing transport', the
    line the connector logs for these drops) is a drop like any other, and re-dials."""

    def ended(self, failure: SocketFailure) -> WatchDropped:
        return WatchDropped(failure.reason)


@dataclass(frozen=True)
class InboxSocket:
    """One opened watch URL, where its messages are exported and what travels the other way."""

    url: str
    exports: Path
    transcript: TranscriptRoute = UnfoundTranscript("no transcript was given to this watch")
    redials: Redials = field(default_factory=Redials)
    source: RunningSource = field(default_factory=RunningSource.current)
    """Where this process's client code came from: an install can replace an installed runtime's,
    never a checkout's."""

    batch_gap_seconds: ClassVar[float] = 0.25
    """Monitor joins the lines of each 200 ms into one event and cuts a long event mid-line: of 11
    notices at once, the last never reached the model though the bus recorded them as taken. A
    local bridge holds such a burst in its window until the transcript shows each one seen; this
    side has no such confirmation, so each message gets a batch of its own."""

    @property
    def ledger(self) -> PrintLedger:
        return self.transcript.ledger(self.exports.parent)

    async def deliver(self, frame: str, socket: ClientWebSocketResponse) -> None:
        """Inline when one Monitor batch holds it, otherwise one notice naming its whole body.
        The ledger is written first: a freeze between it and the print leaves a message the bus
        never replays, never one the session saw twice."""
        message = FieldCodec.decode(Delivered, FieldCodec.parse(frame))
        _, line = ClaudeMonitorFrame(message).event(self.exports)
        self.ledger.record(message.reference)
        print(line, flush=True)
        await socket.send_str(PrintedNow(printed=(message.reference,)).line())
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
        """Deliver until the socket ends, reconnecting on its own after a reset while it can;
        what ended it, for the session to act on."""
        return await (await self.opened(self.url)).settled(self)

    async def opened(self, url: str) -> WatchEnd:
        """One socket's life: how it ended. A failure means what the stage it struck says. Every
        install closes the socket, so a dial is where a replaced runtime is noticed."""
        if self.source.replaced_by_install():
            return RuntimeReplaced()
        stage: SocketStage = Dialing()
        try:
            async with ClientSession() as session, session.ws_connect(
                url, proxy=self.proxy, heartbeat=25.0
            ) as socket:
                stage = Open()
                self.redials.told = self.redials.told.opened()
                for report in self.ledger.reports():
                    await socket.send_str(report)
                await socket.send_str(ReconnectsAlone(printed=()).line())
                publishing = asyncio.create_task(self.publishing(socket))
                try:
                    return await self.frames_until_closed(socket)
                finally:
                    publishing.cancel()
        except (ClientError, OSError) as failure:
            return stage.ended(SocketFailure(failure))

    async def frames_until_closed(self, socket: ClientWebSocketResponse) -> WatchEnd:
        """Every frame until the socket ends, then how it ended. A close frame carries the
        connector's reason, which `async for` swallows: the 1008s of 2026-11-06 printed only a
        number, so nobody could tell maintenance from an ownership conflict."""
        while True:
            frame = await socket.receive()
            match frame.type:
                case WSMsgType.TEXT:
                    await self.received(frame.data, socket)
                case WSMsgType.CLOSE:
                    return WatchEnd.closed(socket.close_code, f": {frame.extra}" if frame.extra else "")
                case WSMsgType.CLOSING | WSMsgType.CLOSED | WSMsgType.ERROR:
                    return WatchEnd.closed(socket.close_code, "")

    async def received(self, frame: str, socket: ClientWebSocketResponse) -> None:
        """A message, or the connector's reconnect URL, which is kept and never shown."""
        if frame.startswith(ReconnectUrl.wire_prefix):
            self.redials.held = ReconnectWith(ReconnectUrl.model_validate_json(frame).url)
            self.redials.told = self.redials.told.reconnectable()
            return
        await self.deliver(frame, socket)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="The one-use URL comms_watch returned")
    exports = Path.home() / ".local" / "state" / "claude-comms-inbox" / "cloud" / "messages"
    socket = InboxSocket(parser.parse_args().url, exports, session_transcript())
    ended = asyncio.run(socket.carried())
    print(ended.line(), flush=True)
    sys.exit(ended.exit_code)
