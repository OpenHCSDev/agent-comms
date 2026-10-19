"""Session-owned endpoint and replaceable, explicitly modeled input readers."""

from __future__ import annotations

import asyncio
import secrets
import socket
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ConfigDict, TypeAdapter, ValidationError

from agent_comms.agent_backend import InputId
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import (
    DeliveredOutcome,
    Outcome,
    TaggedWire,
    UnconfirmedOutcome,
    WireModel,
)
from agent_comms.messages import Message
from agent_comms_hooks.watch import WatchClaim

if TYPE_CHECKING:
    from .shared import IncomingFrame, LineMonitorBridge, MonitorProbe


class PeerOutcome(ABC):
    @abstractmethod
    def write(self, transport: asyncio.Transport, data: bytes) -> bool: ...


class ConnectedPeer(PeerOutcome):
    def write(self, transport: asyncio.Transport, data: bytes) -> bool:
        transport.write(data)
        return True


class GonePeer(PeerOutcome):
    def write(self, transport: asyncio.Transport, data: bytes) -> bool:
        return False


class MonitorPeer(asyncio.Protocol):
    """Own transport loss and closing once; callers consume outcomes, not errors."""

    def __init__(self, service: MonitorService):
        self.service = service
        self.reader = asyncio.StreamReader()
        self.outcome: PeerOutcome = ConnectedPeer()
        self.writable = asyncio.Event()
        self.writable.set()
        self.closed = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.Transport):
            raise TypeError("Monitor requires a byte-stream transport")
        self.transport = transport
        self.service.clients.create_task(self.service.accept(self))

    def data_received(self, data: bytes) -> None:
        self.reader.feed_data(data)

    def connection_lost(self, exc: object) -> None:
        self.outcome = GonePeer()
        self.reader.feed_eof()
        self.writable.set()
        self.closed.set()

    def pause_writing(self) -> None:
        self.writable.clear()

    def resume_writing(self) -> None:
        self.writable.set()

    async def write(self, line: str) -> bool:
        await self.writable.wait()
        return self.outcome.write(self.transport, (line + "\n").encode())

    async def close(self) -> None:
        self.transport.close()
        await self.closed.wait()


class ReaderState:
    """One bridge reader state, including absence and unproven attachment."""

    async def prepare_delivery(self, bridge: LineMonitorBridge) -> ReadyReader:
        async with bridge.reader_changed:
            await bridge.reader_changed.wait_for(lambda: bridge.reader is not self)
        return await bridge.reader.prepare_delivery(bridge)

    def frame(self, bridge: LineMonitorBridge, message: Message) -> IncomingFrame:
        raise ConnectionError("No proven input reader is attached")

    async def send(
        self, bridge: LineMonitorBridge, identity: InputId, payloads: tuple[bytes, ...]
    ) -> type[Outcome]:
        reader = await self.prepare_delivery(bridge)
        return await reader.send(bridge, identity, payloads)

    async def start(self, bridge: LineMonitorBridge) -> None:
        """Already proven input transports need no Monitor challenge."""

    async def receive(self) -> None:
        raise RuntimeError("An absent reader cannot receive input acknowledgments")

    async def detached(self) -> None:
        """A missing reader has no pending input or window."""

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        """A missing or already-proven reader cannot accept a new probe."""

    async def part_observed(self, message_id: str, number: int) -> None:
        """An unattached reader has no flow-control window to release."""

    async def attach(
        self, service: MonitorService, peer: MonitorPeer, request: AttachReader
    ) -> SessionReply:
        return SessionRefused.model_validate(dict(error="This session already has an input reader"))


class NoReader(ReaderState):
    async def attach(
        self, service: MonitorService, peer: MonitorPeer, request: AttachReader
    ) -> SessionReply:
        async with service.bridge.reader_changed:
            if service.bridge.reader is not self:
                return SessionRefused.model_validate(dict(error="Another reader attached first"))
            reader = request.open_reader(service, peer)
            service.bridge.reader = reader
        try:
            if await peer.write(SessionReady().model_dump_json()):
                await reader.start(service.bridge)
                async with service.bridge.reader_changed:
                    service.bridge.reader_changed.notify_all()
                await reader.receive()
            return PeerGone()
        finally:
            async with service.bridge.reader_changed:
                service.bridge.reader = NoReader()
                service.bridge.reader_changed.notify_all()
            await reader.detached()
            print("Input detached; bridge remains connected.", file=sys.stderr, flush=True)


class ReadyReader(ReaderState):
    async def prepare_delivery(self, bridge: LineMonitorBridge) -> ReadyReader:
        return self


@dataclass
class MonitorAttachment:
    peer: MonitorPeer
    probe: MonitorProbe
    pending_parts: dict[tuple[str, int], int] = field(default_factory=dict)
    window_changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, message_id: str, number: int, line: str, window_bytes: int) -> bool:
        size = len((line + "\n").encode("utf-16-le"))
        async with self.window_changed:
            await self.window_changed.wait_for(
                lambda: (
                    self.peer.closed.is_set()
                    or not self.pending_parts
                    or sum(self.pending_parts.values()) + size <= window_bytes
                )
            )
            self.pending_parts[message_id, number] = size
            return await self.peer.write(line)

    async def observed(self, message_id: str, number: int) -> None:
        async with self.window_changed:
            self.pending_parts.pop((message_id, number), None)
            self.window_changed.notify_all()


@dataclass
class UnreadyReader(ReaderState):
    attachment: MonitorAttachment

    async def start(self, bridge: LineMonitorBridge) -> None:
        await self.attachment.peer.write(self.attachment.probe.model_dump_json())

    async def receive(self) -> None:
        await self.attachment.peer.reader.read()

    async def detached(self) -> None:
        async with self.attachment.window_changed:
            self.attachment.window_changed.notify_all()

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        if probe == self.attachment.probe:
            async with bridge.reader_changed:
                bridge.reader = AttachedReader(self.attachment)
                bridge.reader_changed.notify_all()
            print("Fresh Monitor probe observed; delivery enabled.", file=sys.stderr, flush=True)


@dataclass
class AttachedReader(ReadyReader, UnreadyReader):
    def frame(self, bridge: LineMonitorBridge, message: Message) -> IncomingFrame:
        return bridge.incoming_frame(message)

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        """A proven attachment ignores overlapping transcript probe observations."""

    async def part_observed(self, message_id: str, number: int) -> None:
        await self.attachment.observed(message_id, number)

    async def send(
        self, bridge: LineMonitorBridge, identity: InputId, payloads: tuple[bytes, ...]
    ) -> type[Outcome]:
        return await bridge.deliver_to(self.attachment, identity, payloads)


class InputAcknowledgment(WireModel):
    model_config = ConfigDict(frozen=True)
    identity: UUID

    @property
    def input_id(self) -> InputId:
        return InputId(str(self.identity))


@dataclass
class InputReader(ReadyReader):
    peer: MonitorPeer
    pending: dict[InputId, asyncio.Future[type[Outcome]]] = field(default_factory=dict)

    def frame(self, bridge: LineMonitorBridge, message: Message) -> IncomingFrame:
        return bridge.input_frame(message)

    async def send(
        self, bridge: LineMonitorBridge, identity: InputId, payloads: tuple[bytes, ...]
    ) -> type[Outcome]:
        answer: asyncio.Future[type[Outcome]] = asyncio.get_running_loop().create_future()
        self.pending[identity] = answer
        try:
            for payload in payloads:
                if not await self.peer.write(payload.decode().removesuffix("\n")):
                    return UnconfirmedOutcome
            return await answer
        finally:
            del self.pending[identity]

    async def receive(self) -> None:
        async for line in self.peer.reader:
            answer = InputAcknowledgment.model_validate_json(line)
            if answer.input_id in self.pending:
                pending = self.pending[answer.input_id]
                if not pending.done():
                    pending.set_result(DeliveredOutcome)

    async def detached(self) -> None:
        for answer in self.pending.values():
            if not answer.done():
                answer.set_result(UnconfirmedOutcome)


class SessionReply(TaggedWire):
    __registry_config__ = TaggedWire.family_registry()

    def require(self) -> None:
        """Successful replies carry no refusal."""

    async def publish(self, peer: MonitorPeer) -> None:
        await peer.write(self.model_dump_json())

    def end_session(self, service: MonitorService) -> None:
        """Refused requests cannot stop the service."""

    async def attach(
        self, service: MonitorService, peer: MonitorPeer, request: AttachReader
    ) -> SessionReply:
        return self


class SessionReady(SessionReply):
    wire_tag = "ready"

    def end_session(self, service: MonitorService) -> None:
        asyncio.get_running_loop().call_soon(service.stop)

    async def attach(
        self, service: MonitorService, peer: MonitorPeer, request: AttachReader
    ) -> SessionReply:
        return await service.bridge.reader.attach(service, peer, request)


class SessionRefused(SessionReply):
    wire_tag = "refused"
    error: str

    def require(self) -> None:
        raise ValueError(self.error)


class PeerGone(SessionReply):
    wire_tag = "gone"

    async def publish(self, peer: MonitorPeer) -> None:
        """A disconnected peer receives no reply."""


REPLIES = TypeAdapter(SessionReply.declared_union())


class SessionRequest(TaggedWire):
    __registry_config__ = TaggedWire.family_registry()

    def exchange(self, claim: WatchClaim) -> None:
        """Control requests never inject input or retry an emitted event."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect(str(claim.socket_path))
            peer.sendall((self.model_dump_json() + "\n").encode())
            with peer.makefile("rb") as stream:
                REPLIES.validate_json(stream.readline(4096)).require()

    @abstractmethod
    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply: ...


class AttachReader(SessionRequest):
    @abstractmethod
    def open_reader(self, service: MonitorService, peer: MonitorPeer) -> ReaderState: ...


class AttachMonitor(AttachReader):
    wire_tag = "attach"

    def open_reader(self, service: MonitorService, peer: MonitorPeer) -> ReaderState:
        return UnreadyReader(service.bridge.new_attachment(peer, secrets.token_hex(24)))

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return await service.bridge.reader.attach(service, peer, self)


class VerifySession(SessionRequest):
    wire_tag = "verify"
    owner: ProcessIdentity

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        if self.owner != service.owner:
            return SessionRefused.model_validate(
                dict(error="This bridge belongs to a different Claude process incarnation")
            )
        return SessionReady()


class AttachInput(VerifySession, AttachReader):
    wire_tag = "input"

    def open_reader(self, service: MonitorService, peer: MonitorPeer) -> ReaderState:
        return InputReader(peer)

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        verified = await super().handle(service, peer)
        return await verified.attach(service, peer, self)


class EndSession(VerifySession):
    wire_tag = "end"

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        reply = await super().handle(service, peer)
        reply.end_session(service)
        return reply


REQUESTS = TypeAdapter(SessionRequest.declared_union())


@dataclass
class MonitorService:
    bridge: LineMonitorBridge
    owner: ProcessIdentity
    stop: Callable[[], None]
    clients: asyncio.TaskGroup = field(default_factory=asyncio.TaskGroup)

    async def accept(self, peer: MonitorPeer) -> None:
        try:
            line = await peer.reader.readline()
            reply: SessionReply
            try:
                request = REQUESTS.validate_json(line)
            except ValidationError as error:
                reply = SessionRefused.model_validate(dict(error=str(error)))
            else:
                reply = await request.handle(self, peer)
            await reply.publish(peer)
        finally:
            await peer.close()

    async def run(self) -> None:
        path = WatchClaim(self.bridge.binding.thread).socket_path
        path.unlink(missing_ok=True)
        try:
            async with self.clients:
                server = await asyncio.get_running_loop().create_unix_server(
                    lambda: MonitorPeer(self), str(path)
                )
                async with server:
                    print(
                        f"Session bridge ready for Claude PID {self.owner.pid}.",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        await self.owner.wait_exited()
                    finally:
                        self.stop()
        finally:
            path.unlink(missing_ok=True)


async def read_monitor(claim: WatchClaim) -> None:
    with claim.reader_held():
        reader, writer = await asyncio.open_unix_connection(str(claim.socket_path))
        try:
            writer.write((AttachMonitor().model_dump_json() + "\n").encode())
            await writer.drain()
            REPLIES.validate_json(await reader.readline()).require()
            async for line in reader:
                sys.stdout.write(line.decode())
                sys.stdout.flush()
        finally:
            writer.close()
            await writer.wait_closed()
