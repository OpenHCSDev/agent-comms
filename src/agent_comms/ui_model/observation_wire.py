"""Observation transport primitives: peer contracts, relay framing, and terminal outcomes.

Kept free of Core's local service and store imports so remote clients (desktop and Android)
can import ``remote_core``, ``remote_core_process``, and frame relays without pulling in
``agent_comms.comms``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import ClassVar, Protocol

from aiohttp import WSMsgType

from agent_comms.coordination.errors import CoordinationError, ReadAgain
from agent_comms.execution.child_process import RelayEnding
from agent_comms.nominal.field_codec import FieldCodec

WEBSOCKET_HEARTBEAT_SECONDS: float = 25.0
"""How often a connector WebSocket pings its peer; a dead half-open socket closes when pong misses."""
WEBSOCKET_MAX_MESSAGE_BYTES: int = 64 * 1024 * 1024
"""Frame size ceiling for connector WebSockets (sidebars and transcripts exceed aiohttp's 4 MiB default)."""


class WebSocketFrame(Protocol):
    @property
    def type(self) -> WSMsgType: ...

    @property
    def data(self) -> bytes: ...


class WebSocketFrames(Protocol):
    def __aiter__(self) -> AsyncIterator[WebSocketFrame]: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self) -> bool: ...


class ObservationStateTarget(Protocol):
    status: ObserverStatus

    def abandon_reads(self, error: BaseException) -> None: ...


class ObserverStatus(ABC):
    """The reported observer outcome owns whether a request can proceed."""

    @abstractmethod
    def raise_if_failed(self) -> None: ...

    def failure(self, create: Callable[[], ObserverDied]) -> ObserverDied:
        return create()

    def retire(self, process: ObservationStateTarget) -> None:
        pass


class NoObserverFailure(ObserverStatus):
    def raise_if_failed(self) -> None:
        return None

    def retire(self, process: ObservationStateTarget) -> None:
        retired = ObserverRetired("The Comms observation retired before the read was answered")
        process.status = retired
        process.abandon_reads(retired)


class ObservationEnded(CoordinationError):
    """The observation is over, so a request to it cannot be answered.

    The one base both terminal outcomes share: `ObserverRetired` for a process already closed,
    `ObserverDied` (and the connector's `ConnectorObserverClosed`) for one that ended while the
    request was being sent. A caller ending the observation itself is entering this state, not
    failing at it, so it catches this and nothing wider — `CoordinationError` is the whole error
    family, and `ObserverReadFailed` re-raises an arbitrary cause through the same method.
    """


class ObserverRetired(ReadAgain, ObservationEnded, ObserverStatus):
    """This observation process closed and never answers again; the read is dropped, and the
    owner's replacement process reads afresh. Retrying here never answers: no DeferredRead."""

    def raise_if_failed(self) -> None:
        raise self


@dataclass(frozen=True)
class ObserverReadFailed(ObserverStatus):
    error: BaseException

    def raise_if_failed(self) -> None:
        raise self.error

    def settle(self, process: ObservationStateTarget) -> None:
        process.status = self
        process.abandon_reads(self.error)


class ObserverDied(ObservationEnded, ObserverStatus):
    """The observation service ended; reads share its reported cause.

    Presentation and recovery belong to the consuming application; each member says how it
    ended.
    """

    def raise_if_failed(self) -> None:
        raise self

    def failure(self, create: Callable[[], ObserverDied]) -> ObserverDied:
        return self


class ObserverProcessDied(ObserverDied):
    """The local observation process ended; it reports its own pid and how it ended."""

    def __init__(self, process: BaseProcess, error: str = ""):
        self.process, self.error = process, error
        super().__init__(
            f"The comms observer (pid {process.pid}) {self.ending(process)}"
            + (f":\n{error}" if error else "")
        )

    @staticmethod
    def ending(process: BaseProcess) -> str:
        """multiprocessing's exit code, decoded once: a signal, a status, or still running."""
        match process.exitcode:
            case int(status) if status < 0:
                return f"was killed by signal {-status} ({signal.strsignal(-status)})"
            case int(status):
                return f"exited with status {status}"
            case _:
                return "closed its pipe and has not exited"


class RelayConnection(Connection):
    """One side of a relay socket. A relay blocks a worker thread in recv_bytes; closing
    the descriptor from another thread does not wake it, so close shuts the socket down
    first, which ends that read with EOF. A write the other side never takes fails after
    write_timeout_seconds, so a reader that stopped ends the relay instead of pinning it
    and its threads forever."""

    write_timeout_seconds: ClassVar[int] = 30

    def __init__(self, handle: int, readable: bool = True, writable: bool = True) -> None:
        super().__init__(handle, readable, writable)
        with socket.socket(fileno=os.dup(handle)) as peer:
            peer.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_SNDTIMEO,
                struct.pack("ll", self.write_timeout_seconds, 0),
            )

    def close(self) -> None:
        if not self.closed:
            with socket.socket(fileno=os.dup(self.fileno())) as peer:
                peer.shutdown(socket.SHUT_RDWR)
        super().close()


@dataclass(frozen=True)
class LocalSideEnded(RelayEnding):
    """The local socket reached its end of stream."""

    error: BaseException

    def describe(self) -> str:
        return f"the local side ended ({type(self.error).__name__})"


@dataclass(frozen=True)
class LocalSideStoppedReading(RelayEnding):
    """A write to the local socket passed its deadline: that side stopped reading."""

    error: BaseException

    def describe(self) -> str:
        return f"the local side stopped reading ({type(self.error).__name__})"


@dataclass(frozen=True)
class PeerSentFrame(RelayEnding):
    """The peer sent a frame that is not a record."""

    kind: str

    def describe(self) -> str:
        return f"the peer sent a {self.kind} frame"


@dataclass(frozen=True)
class PeerClosed(RelayEnding):
    def describe(self) -> str:
        return "the peer closed its socket"


async def relay_frames(connection: Connection, peer: WebSocketFrames) -> RelayEnding:
    """Carry frames both ways between a local connection and a WebSocket until either side
    ends, then close the WebSocket, stop both directions and say which side ended first.

    The local connection stays open: its owner records the returned reason and then closes
    it, which also wakes a worker thread still blocked on it. Socket reads and writes run in
    worker threads, so a peer that stops reading never blocks this loop. A direction that
    fails in any other way raises its own error here."""
    ended: asyncio.Future[RelayEnding] = asyncio.get_running_loop().create_future()

    def end(ending: RelayEnding) -> None:
        """The first side to end names the relay's end; a later one only follows it."""
        if not ended.done():
            ended.set_result(ending)

    async def outward() -> None:
        try:
            while True:
                await peer.send_bytes(await asyncio.to_thread(connection.recv_bytes))
        except (EOFError, OSError) as error:
            # The local socket's own end-of-stream: this is how its side ends.
            end(LocalSideEnded(error))

    async def inward() -> None:
        try:
            async for message in peer:
                if message.type is not WSMsgType.BINARY:
                    end(PeerSentFrame(message.type.name))
                    return
                await asyncio.to_thread(connection.send_bytes, message.data)
        except OSError as error:
            # The local socket's write deadline: the local side stopped reading.
            end(LocalSideStoppedReading(error))
            return
        end(PeerClosed())

    directions = (asyncio.create_task(outward()), asyncio.create_task(inward()))
    try:
        await asyncio.wait((ended, *directions), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for direction in directions:
            direction.cancel()
        await peer.close()
        await asyncio.wait(directions)
    for direction in directions:
        # A direction that failed rather than ended re-raises its own error here.
        direction.cancelled() or direction.result()
    return ended.result()


class EncodedConnection(Connection):
    """The local pipe's framing and lifetime, carrying declared records instead of pickles,
    so an observation can be served across a boundary the peer does not trust with code.
    Each end declares what it sends and what it receives."""

    sends: ClassVar[type]
    receives: ClassVar[type]

    def send(self, obj: object) -> None:
        self.send_bytes(self.encode(obj))

    def recv(self) -> object:
        return self.decode(self.recv_bytes())

    @classmethod
    def encode(cls, obj: object) -> bytes:
        """Encode through the sending end's own record declaration."""
        return FieldCodec.compact(FieldCodec.encode_document(obj, cls.sends)).encode()

    @classmethod
    def decode(cls, packet: bytes) -> object:
        """Decode through the receiving end's own record declaration."""
        return FieldCodec.decode_document(cls.receives, FieldCodec.parse(packet.decode()))


class ObservationSource(ABC):
    """Something a UI can observe Core through: it supplies the observation's peer."""

    @abstractmethod
    def observation_peer(self, codec: type[EncodedConnection]) -> ObservationPeer: ...


class ObserverEnd(ABC):
    """How a UI learned its observation ended. The peer that served it says what that means:
    a local process reports its exit, a connector its relay's recorded end."""

    @abstractmethod
    def reported_by(self, peer: ObservationPeer) -> ObserverDied: ...


@dataclass(frozen=True)
class ServiceReported(ObserverEnd):
    """The service sent its own traceback before exiting: that is the cause."""

    traceback: str

    def reported_by(self, peer: ObservationPeer) -> ObserverDied:
        return peer.service_failed(self.traceback)


@dataclass(frozen=True)
class ConnectionClosed(ObserverEnd):
    """The connection reached its end without a report from the service."""

    def reported_by(self, peer: ObservationPeer) -> ObserverDied:
        return peer.connection_closed()


@dataclass(frozen=True)
class ConnectionBroken(ObserverEnd):
    """A request found the connection already gone."""

    error: str

    def reported_by(self, peer: ObservationPeer) -> ObserverDied:
        return peer.connection_broken(self.error)


class ObservationPeer(ABC):
    """Who serves a UI's observation: a local process, or a Core across the connector.
    It owns the connection the UI reads, how the service is joined, and how its end reads."""

    connection: Connection

    @abstractmethod
    def join(self) -> None:
        """Wait for the service to end; never ends it."""

    @abstractmethod
    def await_end(self, seconds: float) -> None:
        """Wait up to ``seconds`` for the service to end; never ends it."""

    def retire(self) -> None:
        """End this observation's service and wait for it."""
        self.join()

    def died(self, end: ObserverEnd) -> ObserverDied:
        return end.reported_by(self)

    @abstractmethod
    def service_failed(self, traceback: str) -> ObserverDied: ...

    @abstractmethod
    def connection_closed(self) -> ObserverDied: ...

    def connection_broken(self, error: str) -> ObserverDied:
        """A failed request shows only that the connection is gone: by default, its closing."""
        return self.connection_closed()
