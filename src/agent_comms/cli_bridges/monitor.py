"""Session-owned endpoint and replaceable Monitor readers."""

from __future__ import annotations

import asyncio
import io
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
from types import get_original_bases
from typing import TYPE_CHECKING, Annotated, BinaryIO, ClassVar, NoReturn, get_args, get_origin

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import Outcome
from agent_comms.declared_family import DeclaredFamily
from agent_comms.installer.runtime_wire import CrossesRuntimes
from agent_comms.field_codec import FieldCodec, MalformedRecord, MessageId, PathText
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.messages import Message
from agent_comms.thread_status import ConnectedThreadStatus, PausedBridgeThreadStatus
from agent_comms_hooks.board import SessionBoard, SessionWire
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

    @staticmethod
    def left_stopped(why: str) -> None:
        print(f"Session bridge {why}; left stopped", file=sys.stderr)

    def finish(self, operation: Serve, record: WatchRecord) -> None:
        self.peer.setblocking(True)
        try:
            with self.peer, self.peer.makefile("rwb", buffering=0) as stream:
                ready = FieldCodec.compact(FieldCodec.encode(SessionReady()))
                stream.write((ready + "\n").encode())
                while operation.owner.alive():
                    if select.select((self.peer,), (), (), 1)[0]:
                        break
                else:
                    self.left_stopped(f"owner {operation.owner.pid} exited")
                    return
                self.peer.settimeout(30)
                raw = stream.readline(4096)
                if not raw:
                    raise ConnectionResetError("Installer closed before selecting a runtime")
                runtime = FieldCodec.decode(LaunchRuntime, FieldCodec.parse(raw.decode()))
        except ConnectionError:
            # A broken pipe or EOF means the installer died. Never select a fallback runtime.
            self.left_stopped("installer gone before ready")
            return
        if not operation.owner.alive():
            self.left_stopped(f"owner {operation.owner.pid} exited")
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

    async def part_observed(self, message_id: MessageId, number: int) -> None:
        """Only an attached reader has a flow-control window."""

    async def attach(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return SessionRefused(error="This session already has a Monitor reader")


class NoReader(ReaderState):
    async def attach(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        bridge = service.bridge
        attachment = bridge.new_attachment(peer, secrets.token_hex(24))
        async with bridge.reader_changed:
            bridge.reader = UnreadyReader(attachment)
        try:
            await SessionReady().publish(peer)
            await peer.write(attachment.probe.model_dump_json())
            await peer.reader.read()
            # Only a reader leaving a bridge that keeps serving is declared. A stopping bridge
            # skips this registry write: its install pause is acknowledged only once teardown
            # ends, and a successor declares its own reader state when it starts serving.
            async with bridge.reader_changed:
                bridge.reader = NoReader()
                await bridge.declare_reader(PausedBridgeThreadStatus())
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
    pending_parts: dict[tuple[MessageId, int], int] = field(default_factory=dict)
    window_changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, message_id: MessageId, number: int, line: str, window_bytes: int) -> None:
        size = len((line + "\n").encode("utf-16-le"))
        async with self.window_changed:
            await self.window_changed.wait_for(
                lambda: self.peer.writer.is_closing()
                or not self.pending_parts
                or sum(self.pending_parts.values()) + size <= window_bytes
            )
            self.pending_parts[message_id, number] = size
            await self.peer.write(line)

    async def observed(self, message_id: MessageId, number: int) -> None:
        async with self.window_changed:
            self.pending_parts.pop((message_id, number), None)
            self.window_changed.notify_all()


@dataclass
class UnreadyReader(ReaderState):
    attachment: MonitorAttachment

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        if probe == self.attachment.probe:
            async with bridge.reader_changed:
                if bridge.reader is not self:
                    return
                await bridge.declare_reader(ConnectedThreadStatus())
                bridge.reader = AttachedReader(self.attachment)
                bridge.reader_changed.notify_all()
            bridge.log("Fresh Monitor probe observed; delivery enabled.")


@dataclass
class AttachedReader(UnreadyReader):
    async def prepare_delivery(self, bridge: LineMonitorBridge) -> AttachedReader:
        return self

    async def confirm_probe(self, bridge: LineMonitorBridge, probe: MonitorProbe) -> None:
        """Overlapping observations cannot prove a different attachment."""

    async def part_observed(self, message_id: MessageId, number: int) -> None:
        await self.attachment.observed(message_id, number)

    async def deliver(self, bridge: LineMonitorBridge, message: Message) -> type[Outcome]:
        return await bridge.deliver_to(self.attachment, message)


@dataclass(frozen=True)
class SessionReply(SessionWire, DeclaredFamily, affix=""):

    def require(self) -> None:
        """Successful replies carry no refusal."""

    async def publish(self, peer: MonitorPeer) -> None:
        await peer.write(FieldCodec.compact(FieldCodec.encode(self)))

    def end_session(self, service: MonitorService) -> None:
        """A refusal cannot stop the service."""

    def pause_session(self, service: MonitorService, peer: MonitorPeer,
                      process: ProcessIdentity) -> SessionReply:
        return self


@dataclass(frozen=True)
class SessionReady(SessionReply, declared_name='ready'):

    def end_session(self, service: MonitorService) -> None:
        asyncio.get_running_loop().call_soon(service.stop)

    def pause_session(self, service: MonitorService, peer: MonitorPeer,
                      process: ProcessIdentity) -> SessionReply:
        if process != ProcessIdentity.capture(os.getpid()):
            return SessionRefused(error='This is a different bridge process incarnation')
        service.bridge.retirement = BridgeReplacement(peer.writer.get_extra_info('socket').dup())
        self.end_session(service)
        return PeerGone()


@dataclass(frozen=True)
class SessionInformation(SessionReply, declared_name='information'):
    owner: ProcessIdentity
    process: ProcessIdentity
    runtime: LaunchRuntime
    transcript: Annotated[Path, PathText]


@dataclass(frozen=True)
class SessionRefused(SessionReply, declared_name='refused'):
    error: str

    def require(self) -> NoReturn:
        raise SessionRefusedError(self.error)


@dataclass(frozen=True)
class BoardReply(SessionBoard, SessionReply, declared_name=SessionBoard.request):
    """The hook decodes this with the standard library, through the fields it declares."""


@dataclass(frozen=True)
class PeerGone(SessionReply, declared_name='gone'):

    async def publish(self, peer: MonitorPeer) -> None:
        """The stream already ended; there is no reply to send."""


@dataclass(frozen=True)
class SessionRequest[Reply: SessionReply](SessionWire, DeclaredFamily, affix=""):
    reply_class: ClassVar[type[SessionReply]]
    reply_timeout: ClassVar[float] = 30

    def __init_subclass__(cls, **options):
        super().__init_subclass__(**options)
        cls.reply_class = next(
            get_args(base)[0]
            for owner in cls.__mro__
            for base in get_original_bases(owner)
            if get_origin(base) is SessionRequest
        )

    @contextmanager
    def connection(self, endpoint: Path) -> Iterator[socket.socket]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(self.reply_timeout)
            peer.connect(str(endpoint))
            peer.sendall((FieldCodec.compact(FieldCodec.encode(self)) + "\n").encode())
            yield peer

    def exchange(self, endpoint: Path) -> Reply:
        with self.connection(endpoint) as peer, peer.makefile("rb") as stream:
            return self.read_reply(stream)

    def read_reply(self, stream: BinaryIO) -> Reply:
        """The bridge's one answer line; a socket closed before it is no answer at all."""
        raw = stream.readline(4096)
        if not raw:
            raise ConnectionResetError('Session bridge closed before replying')
        reply = FieldCodec.decode(self.reply_class | SessionRefused, FieldCodec.parse(raw))
        reply.require()
        return reply

    @abstractmethod
    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply: ...


@dataclass(frozen=True)
class AttachMonitor(CrossesRuntimes, SessionRequest[SessionReady], declared_name='attach'):

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return await service.bridge.reader.attach(service, peer)


@dataclass(frozen=True)
class ReadBoard(SessionRequest[BoardReply], declared_name=SessionBoard.request):
    """The commander's tool-call hook asking for its board at the moment of the call."""

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return BoardReply(await service.bridge.board())


@dataclass(frozen=True)
class VerifySession(CrossesRuntimes, SessionRequest[SessionReady], declared_name='verify'):
    owner: ProcessIdentity

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        if self.owner != service.owner or not service.owner.alive():
            return SessionRefused(error="This bridge belongs to a different or exited Claude process incarnation")
        return SessionReady()


@dataclass(frozen=True)
class InspectSession(CrossesRuntimes, SessionRequest[SessionInformation], declared_name='inspect'):

    def information(self, endpoint: Path) -> SessionInformation:
        return self.exchange(endpoint)

    def awaited(
        self, claim: WatchClaim, swapping: Callable[[WatchClaim], None],
    ) -> SessionInformation:
        """An install swaps the endpoint; the claim's state says whether an absent socket is
        that gap or gone."""
        named = False
        while True:
            try:
                return self.information(claim.socket_path)
            except (FileNotFoundError, ConnectionRefusedError) as absent:
                state = claim.state()
                named = named or state.name_wait(lambda: swapping(claim))
                state.await_socket(claim, absent)

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        return SessionInformation(
            owner=service.owner, process=ProcessIdentity.capture(os.getpid()),
            runtime=LaunchRuntime.running(),
            transcript=service.bridge.options.operation.transcript_source(),
        )


@dataclass(frozen=True)
class PauseSession(VerifySession, declared_name='pause'):
    process: ProcessIdentity

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        reply = await super().handle(service, peer)
        # The replacement acknowledges only after all bus tasks, locks and DBs close.
        return reply.pause_session(service, peer, self.process)


@dataclass(frozen=True)
class EndSession(VerifySession, declared_name='end'):

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        reply = await super().handle(service, peer)
        reply.end_session(service)
        return reply


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
                    request = FieldCodec.decode(SessionRequest, FieldCodec.parse(await reader.readline()))
                except MalformedRecord as error:
                    reply = SessionRefused(error=str(error))
                else:
                    reply = await request.handle(self, peer)
                await reply.publish(peer)
            finally:
                await peer.close()
        except (BrokenPipeError, ConnectionResetError) as error:
            await self.bridge.report_failure(f"Monitor connection ended: {error}")

    async def run(self) -> None:
        await self.bridge.declare_reader(PausedBridgeThreadStatus())
        path = WatchClaim(self.bridge.binding.thread).socket_path
        path.unlink(missing_ok=True)
        try:
            server = await asyncio.start_unix_server(
                lambda reader, writer: clients.create_task(self.accept(reader, writer)),
                str(path), start_serving=False,
            )
            async with server:
                try:
                    async with asyncio.TaskGroup() as clients:
                        await server.start_serving()
                        self.bridge.log(f"Session bridge ready for Claude PID {self.owner.pid}.")
                        try:
                            await self.owner.wait_exited()
                        finally:
                            # The listener stops before the clients retire, so no new
                            # connection reaches a group that is shutting down.
                            server.close()
                            self.stop()
                finally:
                    # A connection accepted in the same loop step as close has no client
                    # task; closing it keeps Server.wait_closed from waiting forever.
                    server.close_clients()
        finally:
            path.unlink(missing_ok=True)


async def read_monitor(
    claim: WatchClaim, announce: Callable[[WatchClaim], None],
    swapping: Callable[[WatchClaim], None],
) -> None:
    session = await asyncio.to_thread(InspectSession().awaited, claim, swapping)
    announced = False
    with claim.reader_held():
        while session.owner.alive():
            try:
                current = await asyncio.to_thread(InspectSession().information, claim.socket_path)
                if current.owner != session.owner:
                    raise ValueError('The session bridge now belongs to a different owner')
                reader, writer = await asyncio.open_unix_connection(str(claim.socket_path))
                peer = MonitorPeer(reader, writer)
                try:
                    request = AttachMonitor()
                    await peer.write(FieldCodec.compact(FieldCodec.encode(request)))
                    request.read_reply(io.BytesIO(await reader.readline()))
                    announced = False
                    async for line in reader:
                        sys.stdout.write(line.decode())
                        sys.stdout.flush()
                finally:
                    await peer.close()
            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
                # Retirement drops the old connection; the same owner can publish a
                # fresh endpoint, and this reader keeps its lease for it. A stopped
                # bridge is still an outage, so the claim's state decides whether to say so.
                announced = announced or claim.state().announce_absence(
                    lambda: announce(claim))
            await asyncio.sleep(.05)
