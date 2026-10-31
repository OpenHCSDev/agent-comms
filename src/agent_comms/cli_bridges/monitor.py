"""Session-owned endpoint and replaceable Monitor readers."""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import select
import socket
import sys
import traceback
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import get_original_bases
from typing import TYPE_CHECKING, Annotated, BinaryIO, ClassVar, NoReturn, Self, get_args, get_origin

from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination_errors import CoordinationError
from agent_comms.harness_delivery_states import Outcome
from agent_comms.declared_family import DeclaredFamily
from agent_comms.installer.runtime_wire import CrossesRuntimes
from agent_comms.field_codec import FieldCodec, MalformedRecord, MessageId, PathText
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.messages import Message
from agent_comms.thread_status import ConnectedThreadStatus, PausedBridgeThreadStatus
from agent_comms_hooks.board import SessionBoard, SessionWire
from agent_comms_hooks.watch import SessionFailure

from .session_watch import (
    HeldClaim,
    SessionConnectionClosed,
    SessionRefusedError,
    SessionRequestTimeout,
    WatchClaim,
    WatchRecord,
)


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

    async def read(self) -> bytes:
        """One request line, including tool inputs larger than StreamReader's buffer.

        The stream limit is a buffering threshold, not a hook-payload limit. Letting
        LimitOverrunError escape would tear down the resident bridge's TaskGroup.
        """
        chunks: list[bytes] = []
        async with asyncio.timeout(SessionRequest.reply_timeout):
            while True:
                try:
                    chunks.append(await self.reader.readuntil(b"\n"))
                    return b"".join(chunks)
                except asyncio.LimitOverrunError as partial:
                    chunks.append(await self.reader.readexactly(partial.consumed))
                except asyncio.IncompleteReadError as ended:
                    chunks.append(ended.partial)
                    return b"".join(chunks)

    async def write(self, line: str) -> None:
        try:
            self.writer.write((line + "\n").encode())
            await self.writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            # A failed output half cannot keep the attachment's input read alive.
            # Closing wakes that read; its existing finally releases the reader slot.
            self.writer.close()
            raise

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
            bridge.log(f"Monitor detached; exception={sys.exception()!r} "
                       "(None means reader EOF); delivery paused.")


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

    def require(self) -> Self:
        """A successful reply keeps its declared type; a refusal never returns."""
        return self

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
class RequestFailure:
    """A failed request keeps its exception until it reports a refused wire reply."""

    error: Exception

    def reply(self, log: Callable[[str], None]) -> SessionRefused:
        failure = "".join(traceback.format_exception(self.error))
        log(f"Session request failed:\n{failure}")
        return SessionRefused(error=failure)


@dataclass(frozen=True)
class BoardReply(SessionBoard, SessionReply, declared_name=SessionBoard.request):
    """The hook decodes this with the standard library, through the fields it declares."""


@dataclass(frozen=True)
class PeerGone(SessionReply, declared_name='gone'):

    async def publish(self, peer: MonitorPeer) -> None:
        """The stream already ended; there is no reply to send."""


@dataclass(frozen=True)
class SessionRequest[Reply: SessionReply](SessionWire, DeclaredFamily, affix=""):
    reply_timeout: ClassVar[float] = 30

    @classmethod
    def reply_type(cls) -> type[Reply]:
        return cls.declared_argument(SessionRequest)

    @classmethod
    def declared_argument(cls, family: type) -> type:
        """A request capability derives its payload from its generic declaration."""
        return next(
            get_args(base)[0]
            for owner in cls.__mro__
            for base in get_original_bases(owner)
            if get_origin(base) is family
        )

    @classmethod
    def from_wire_line(cls, raw: bytes) -> Self:
        """The request boundary parses and constructs off the session event loop."""
        return FieldCodec.decode(cls, FieldCodec.parse(raw))

    def line(self) -> str:
        return FieldCodec.compact(FieldCodec.encode(self))

    @contextmanager
    def _exchange_errors(self) -> Iterator[None]:
        try:
            yield
        except TimeoutError as error:
            raise SessionRequestTimeout(
                f'Session {self.declared_name} request timed out after '
                f'{self.reply_timeout:g} seconds') from error
        except (BrokenPipeError, ConnectionResetError) as error:
            raise SessionConnectionClosed(
                f'Session {self.declared_name} connection closed before completing its reply') from error

    @contextmanager
    def connection(self, endpoint: Path) -> Iterator[socket.socket]:
        with self._exchange_errors(), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(self.reply_timeout)
            peer.connect(str(endpoint))
            peer.sendall((self.line() + "\n").encode())
            yield peer

    @asynccontextmanager
    async def replying(self, endpoint: Path, resources: AsyncExitStack) -> AsyncIterator[MonitorPeer]:
        """Connect, reply and the caller's startup share this request's one budget."""
        with self._exchange_errors():
            async with asyncio.timeout(self.reply_timeout):
                reader, writer = await asyncio.open_unix_connection(str(endpoint))
                peer = MonitorPeer(reader, writer)
                resources.push_async_callback(peer.close)
                await peer.write(self.line())
                self.read_reply(io.BytesIO(await reader.readline()))
                yield peer

    def exchange(self, endpoint: Path) -> Reply:
        with self.connection(endpoint) as peer, peer.makefile("rb") as stream:
            return self.read_reply(stream)

    def read_reply(self, stream: BinaryIO) -> Reply:
        """The bridge's one answer line; a socket closed before it is no answer at all."""
        with self._exchange_errors():
            raw = stream.readline()
        if not raw:
            raise SessionConnectionClosed('Session bridge closed before replying')
        reply = FieldCodec.decode(self.reply_type() | SessionRefused, FieldCodec.parse(raw))
        return reply.require()

    @abstractmethod
    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply: ...


@dataclass(frozen=True)
class AttachMonitor(CrossesRuntimes, SessionRequest[SessionReady], declared_name='attach'):

    async def lines(self, endpoint: Path) -> AsyncIterator[bytes]:
        """Bound the acknowledgement and startup probe, not the established live stream."""
        async with AsyncExitStack() as resources:
            async with self.replying(endpoint, resources) as peer:
                first = await peer.reader.readline()
                if not first:
                    raise SessionConnectionClosed('Session bridge closed before its Monitor startup probe')
            yield first
            async for line in peer.reader:
                yield line

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


BRIDGE_FAILURES: tuple[type[Exception], ...] = (OSError, ValueError, CoordinationError)
"""What the bus, its files and its wire can raise into a bridge: the failures a bridge reports.
Anything else is a defect in the bridge's own code and crashes it loudly."""


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
                    request = await asyncio.to_thread(SessionRequest.from_wire_line, await peer.read())
                    reply = await request.handle(self, peer)
                except (MalformedRecord, TimeoutError, SessionFailure) as error:
                    reply = SessionRefused(error=str(error))
                except BRIDGE_FAILURES as error:
                    # Refuse this request without cancelling every client in the TaskGroup.
                    reply = RequestFailure(error).reply(self.bridge.log)
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


async def relaunched(claim: WatchClaim, relaunch: Callable[[], None]) -> bool:
    """Whether the claim's state took this reader's relaunch. A bridge that cannot start is the
    outage the reader then announces, naming its log; it is not retried."""
    try:
        return await asyncio.to_thread(claim.state().rearm, relaunch)
    except (SessionFailure, *BRIDGE_FAILURES) as error:
        print(f'Session bridge relaunch failed: {error}', file=sys.stderr, flush=True)
        return False


async def read_monitor(
    claim: WatchClaim, announce: Callable[[WatchClaim], None],
    swapping: Callable[[WatchClaim], None],
    relaunch: Callable[[WatchClaim, SessionInformation], None],
) -> None:
    """Holds the reader claim and relays the bridge's lines. A bridge killed from under the
    session comes back through its claim. Only a kill leaves its socket file behind: a bridge
    that stopped on its own removed it and stays stopped, loudly. Only a bridge that then
    delivered something earns another relaunch, so one dying on start cannot loop."""
    session = await asyncio.to_thread(InspectSession().awaited, claim, swapping)
    announced = False
    delivering = True
    with claim.reader_held():
        while session.owner.alive():
            try:
                current = await asyncio.to_thread(InspectSession().information, claim.socket_path)
                if current.owner != session.owner:
                    raise ValueError('The session bridge now belongs to a different owner')
                announced = False
                probed = False
                async for line in AttachMonitor().lines(claim.socket_path):
                    sys.stdout.write(line.decode())
                    sys.stdout.flush()
                    delivering = delivering or probed
                    probed = True
            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
                # Retirement drops the old connection; the same owner can publish a
                # fresh endpoint, and this reader keeps its lease for it. A stopped
                # bridge is still an outage, so the claim's state decides whether to say so.
                if (delivering and claim.socket_path.exists()
                        and await relaunched(claim, partial(relaunch, claim, session))):
                    delivering = False
                    continue
                announced = announced or claim.state().announce_absence(
                    lambda: announce(claim))
            await asyncio.sleep(.05)
