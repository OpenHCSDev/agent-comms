"""Session-owned endpoint and replaceable Monitor readers."""

from __future__ import annotations

import asyncio
import secrets
import socket
import sys
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import Outcome, TaggedWire
from agent_comms.messages import Message
from agent_comms_hooks.watch import WatchClaim

if TYPE_CHECKING:
    from .shared import LineMonitorBridge, MonitorProbe


@dataclass
class MonitorPeer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def write(self, line: str) -> None:
        self.writer.write((line + "\n").encode())
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


class ReaderState:
    async def prepare_delivery(self, bridge: LineMonitorBridge) -> AttachedReader:
        async with bridge.reader_changed:
            await bridge.reader_changed.wait_for(lambda: bridge.reader is not self)
        return await bridge.reader.prepare_delivery(bridge)

    async def deliver(self, bridge: LineMonitorBridge, message: Message) -> type[Outcome]:
        reader = await self.prepare_delivery(bridge)
        return await reader.deliver(bridge, message)

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        """Only an unproven attachment consumes a probe."""

    async def part_observed(self, message_id: str, number: int) -> None:
        """Only an attached reader has a flow-control window."""

    async def attach(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return SessionRefused.model_validate(
            dict(error="This session already has a Monitor reader")
        )


class NoReader(ReaderState):
    async def attach(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        bridge = service.bridge
        attachment = bridge.new_attachment(peer, secrets.token_hex(24))
        async with bridge.reader_changed:
            bridge.reader = UnreadyReader(attachment)
        try:
            await peer.write(SessionReady().model_dump_json())
            await peer.write(attachment.probe.model_dump_json())
            await peer.reader.read()
            return PeerGone()
        finally:
            async with bridge.reader_changed:
                bridge.reader = NoReader()
                bridge.reader_changed.notify_all()
            async with attachment.window_changed:
                attachment.window_changed.notify_all()
            print(
                "Monitor detached; delivery paused, bridge remains connected.",
                file=sys.stderr,
                flush=True,
            )


@dataclass
class MonitorAttachment:
    peer: MonitorPeer
    probe: MonitorProbe
    pending_parts: dict[tuple[str, int], int] = field(default_factory=dict)
    window_changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, message_id: str, number: int, line: str, window_bytes: int) -> None:
        size = len((line + "\n").encode("utf-16-le"))
        async with self.window_changed:
            await self.window_changed.wait_for(
                lambda: self.peer.writer.is_closing()
                or not self.pending_parts
                or sum(self.pending_parts.values()) + size <= window_bytes
            )
            self.pending_parts[message_id, number] = size
            await self.peer.write(line)

    async def observed(self, message_id: str, number: int) -> None:
        async with self.window_changed:
            self.pending_parts.pop((message_id, number), None)
            self.window_changed.notify_all()


@dataclass
class UnreadyReader(ReaderState):
    attachment: MonitorAttachment

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        if probe == self.attachment.probe:
            async with bridge.reader_changed:
                bridge.reader = AttachedReader(self.attachment)
                bridge.reader_changed.notify_all()
            print("Fresh Monitor probe observed; delivery enabled.", file=sys.stderr, flush=True)


@dataclass
class AttachedReader(UnreadyReader):
    async def prepare_delivery(self, bridge: LineMonitorBridge) -> AttachedReader:
        return self

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        """Overlapping observations cannot prove a different attachment."""

    async def part_observed(self, message_id: str, number: int) -> None:
        await self.attachment.observed(message_id, number)

    async def deliver(self, bridge: LineMonitorBridge, message: Message) -> type[Outcome]:
        return await bridge.deliver_to(self.attachment, message)


class SessionReply(TaggedWire):
    __registry_config__ = TaggedWire.family_registry()

    def require(self) -> None:
        """Successful replies carry no refusal."""

    async def publish(self, peer: MonitorPeer) -> None:
        await peer.write(self.model_dump_json())

    def end_session(self, service: MonitorService) -> None:
        """A refusal cannot stop the service."""


class SessionReady(SessionReply):
    wire_tag = "ready"

    def end_session(self, service: MonitorService) -> None:
        asyncio.get_running_loop().call_soon(service.stop)


class SessionRefused(SessionReply):
    wire_tag = "refused"
    error: str

    def require(self) -> None:
        raise ValueError(self.error)


class PeerGone(SessionReply):
    wire_tag = "gone"

    async def publish(self, peer: MonitorPeer) -> None:
        """The stream already ended; there is no reply to send."""


REPLIES = TypeAdapter(SessionReply.declared_union())


class SessionRequest(TaggedWire):
    __registry_config__ = TaggedWire.family_registry()

    def exchange(self, claim: WatchClaim) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect(str(claim.socket_path))
            peer.sendall((self.model_dump_json() + "\n").encode())
            with peer.makefile("rb") as stream:
                REPLIES.validate_json(stream.readline(4096)).require()

    @abstractmethod
    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply: ...


class AttachMonitor(SessionRequest):
    wire_tag = "attach"

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return await service.bridge.reader.attach(service, peer)


class VerifySession(SessionRequest):
    wire_tag = "verify"
    owner: ProcessIdentity

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        if self.owner != service.owner:
            return SessionRefused.model_validate(
                dict(error="This bridge belongs to a different Claude process incarnation")
            )
        return SessionReady()


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

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = MonitorPeer(reader, writer)
        try:
            try:
                reply: SessionReply
                try:
                    request = REQUESTS.validate_json(await reader.readline())
                except ValidationError as error:
                    reply = SessionRefused.model_validate(dict(error=str(error)))
                else:
                    reply = await request.handle(self, peer)
                await reply.publish(peer)
            finally:
                await peer.close()
        except (BrokenPipeError, ConnectionResetError) as error:
            await self.bridge.report_failure(f"Monitor connection ended: {error}")

    async def run(self) -> None:
        path = WatchClaim(self.bridge.binding.thread).socket_path
        path.unlink(missing_ok=True)
        try:
            async with asyncio.TaskGroup() as clients:
                server = await asyncio.start_unix_server(
                    lambda reader, writer: clients.create_task(self.accept(reader, writer)),
                    str(path),
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
        peer = MonitorPeer(reader, writer)
        try:
            await peer.write(AttachMonitor().model_dump_json())
            REPLIES.validate_json(await reader.readline()).require()
            async for line in reader:
                sys.stdout.write(line.decode())
                sys.stdout.flush()
        finally:
            await peer.close()
