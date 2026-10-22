"""Session-owned endpoint and replaceable Monitor readers."""

from __future__ import annotations

import asyncio
import os
import secrets
import select
import socket
import sys
from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import Outcome, TaggedWire
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.messages import Message
from agent_comms_hooks.watch import HeldClaim, SessionRefusedError, WatchClaim, WatchRecord

if TYPE_CHECKING:
    from .shared import LineMonitorBridge, MonitorProbe, Serve


class BridgeExit:
    """Normal retirement has no successor runtime."""

    def keeping(self, held: HeldClaim) -> BridgeExit:
        """Without a successor the claim ends with this process."""
        return self

    def finish(self, operation: Serve, record: WatchRecord) -> None:
        pass


@dataclass
class BridgeReplacement(BridgeExit):
    """An install asked for a successor; the claim is not yet kept for it."""

    peer: socket.socket

    def keeping(self, held: HeldClaim) -> ClaimedReplacement:
        return ClaimedReplacement(self.peer, held.inheritable())


@dataclass
class ClaimedReplacement(BridgeReplacement):
    """Keep the owner-launched process and its session claim, not its old bus resources,
    through install. The claim's locked description passes to the successor through exec,
    so no session hook ever sees the claim lapse and launches a second bridge."""

    claim: int

    def finish(self, operation: Serve, record: WatchRecord) -> None:
        self.peer.setblocking(True)
        with self.peer, self.peer.makefile("rwb", buffering=0) as stream:
            stream.write((SessionReady().model_dump_json() + "\n").encode())
            while operation.owner.alive():
                if select.select((self.peer,), (), (), 1)[0]:
                    break
            else:
                print(f"Session bridge owner {operation.owner.pid} exited; left stopped",
                      file=sys.stderr)
                return
            # EOF means the installer died. Never select a fallback runtime.
            self.peer.settimeout(30)
            raw = stream.readline(4096)
            if not raw:
                return
            runtime = FieldCodec.decode(LaunchRuntime, FieldCodec.parse(raw.decode()))
        if not operation.owner.alive():
            print(f"Session bridge owner {operation.owner.pid} exited; left stopped",
                  file=sys.stderr)
            return
        operation.owner.require_alive()
        runtime.require_available()
        command = operation.successor(self.claim).command(record, runtime)
        os.execve(command[0], command, runtime.environment(os.environ))


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
            bridge.log("Monitor detached; delivery paused, bridge remains connected.")


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
            bridge.log("Fresh Monitor probe observed; delivery enabled.")


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

    def pause_session(self, service: MonitorService, peer: MonitorPeer,
                      process: ProcessIdentity) -> SessionReply:
        return self


class SessionReady(SessionReply):
    wire_tag = "ready"

    def end_session(self, service: MonitorService) -> None:
        asyncio.get_running_loop().call_soon(service.stop)

    def pause_session(self, service: MonitorService, peer: MonitorPeer,
                      process: ProcessIdentity) -> SessionReply:
        if process != ProcessIdentity.capture(os.getpid()):
            return SessionRefused(error='This is a different bridge process incarnation')
        service.bridge.retirement = BridgeReplacement(peer.writer.get_extra_info('socket').dup())
        self.end_session(service)
        return PeerGone()


class SessionInformation(SessionReply):
    wire_tag = "information"
    owner: ProcessIdentity
    process: ProcessIdentity
    runtime: LaunchRuntime
    transcript: Path


class SessionRefused(SessionReply):
    wire_tag = "refused"
    error: str

    def require(self) -> None:
        raise SessionRefusedError(self.error)


class PeerGone(SessionReply):
    wire_tag = "gone"

    async def publish(self, peer: MonitorPeer) -> None:
        """The stream already ended; there is no reply to send."""


REPLIES = TypeAdapter(SessionReply.declared_union())


class SessionRequest(TaggedWire):
    __registry_config__ = TaggedWire.family_registry()

    @contextmanager
    def connection(self, claim: WatchClaim) -> Iterator[socket.socket]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(30)
            peer.connect(str(claim.socket_path))
            peer.sendall((self.model_dump_json() + "\n").encode())
            yield peer

    def exchange(self, claim: WatchClaim) -> SessionReply:
        with self.connection(claim) as peer, peer.makefile("rb") as stream:
            raw = stream.readline(4096)
            if not raw:
                raise ConnectionResetError('Session bridge closed before replying')
            reply = REPLIES.validate_json(raw)
            reply.require()
            return reply

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
        if self.owner != service.owner or not service.owner.alive():
            return SessionRefused.model_validate(
                dict(error="This bridge belongs to a different or exited Claude process incarnation")
            )
        return SessionReady()


class InspectSession(SessionRequest):
    wire_tag = "inspect"

    def information(self, claim: WatchClaim) -> SessionInformation:
        reply = self.exchange(claim)
        if not isinstance(reply, SessionInformation):
            raise ValueError("Session bridge did not report its process and runtime")
        return reply

    def awaited(self, claim: WatchClaim) -> SessionInformation:
        """An install swaps the endpoint; the claim's state says whether an absent socket is
        that gap or gone."""
        while True:
            try:
                return self.information(claim)
            except (FileNotFoundError, ConnectionRefusedError) as absent:
                claim.state().await_socket(absent)

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return SessionInformation(
            owner=service.owner, process=ProcessIdentity.capture(os.getpid()),
            runtime=LaunchRuntime.running(),
            transcript=service.bridge.options.operation.transcript_source(),
        )


class PauseSession(VerifySession):
    wire_tag = "pause"
    process: ProcessIdentity

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        reply = await super().handle(service, peer)
        # The replacement acknowledges only after all bus tasks, locks and DBs close.
        return reply.pause_session(service, peer, self.process)


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
            server = await asyncio.start_unix_server(
                lambda reader, writer: clients.create_task(self.accept(reader, writer)),
                str(path), start_serving=False,
            )
            # Clients retire before Server.wait_closed waits for their transports.
            async with server, asyncio.TaskGroup() as clients:
                await server.start_serving()
                self.bridge.log(f"Session bridge ready for Claude PID {self.owner.pid}.")
                try:
                    await self.owner.wait_exited()
                finally:
                    self.stop()
        finally:
            path.unlink(missing_ok=True)


async def read_monitor(claim: WatchClaim) -> None:
    session = await asyncio.to_thread(InspectSession().awaited, claim)
    with claim.reader_held():
        while session.owner.alive():
            try:
                current = await asyncio.to_thread(InspectSession().information, claim)
                if current.owner != session.owner:
                    raise ValueError('The session bridge now belongs to a different owner')
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
            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
                pass
            # Retirement drops the old connection; the same owner can publish a
            # fresh endpoint. Keep the reader lease and require its fresh probe.
            await asyncio.sleep(.05)
