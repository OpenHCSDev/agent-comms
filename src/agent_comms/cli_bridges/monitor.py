"""One session-owned bridge endpoint; Monitor calls only borrow a reader."""
from __future__ import annotations

import asyncio
import secrets
import socket
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from metaclass_registry import RegistryConfig
from pydantic import TypeAdapter

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import TaggedWire
from agent_comms_hooks.watch import WatchClaim

if TYPE_CHECKING:
    from .shared import LineMonitorBridge, MonitorProbe


@dataclass
class MonitorAttachment:
    """A probe and flow-control window belong to one actual socket reader."""

    writer: asyncio.StreamWriter
    probe: MonitorProbe
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    pending_parts: dict[tuple[str, int], int] = field(default_factory=dict)
    window_changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    @classmethod
    def opened(cls, writer: asyncio.StreamWriter, session: str) -> MonitorAttachment:
        from .shared import MonitorProbe
        return cls(writer, MonitorProbe(session=session, token=secrets.token_hex(24)))

    async def write(self, line: str) -> None:
        if self.writer.is_closing():
            raise ConnectionResetError('The Monitor reader disconnected')
        self.writer.write((line + '\n').encode())
        await self.writer.drain()

    async def emit(self, message_id: str, number: int, line: str, window_bytes: int) -> None:
        async with self.window_changed:
            await self.window_changed.wait_for(
                lambda: self.writer.is_closing() or not self.pending_parts
                or sum(self.pending_parts.values()) + len((line + '\n').encode('utf-16-le')) <= window_bytes)
            self.pending_parts[message_id, number] = len((line + '\n').encode('utf-16-le'))
            await self.write(line)

    async def observed(self, message_id: str, number: int) -> None:
        async with self.window_changed:
            self.pending_parts.pop((message_id, number), None)
            self.window_changed.notify_all()

    async def close(self) -> None:
        self.writer.close()
        async with self.window_changed:
            self.window_changed.notify_all()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


class SessionReply(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    @abstractmethod
    def require(self) -> None: ...


class SessionReady(SessionReply):
    wire_tag = 'ready'

    def require(self) -> None:
        pass


class SessionRefused(SessionReply):
    wire_tag = 'refused'
    error: str

    def require(self) -> None:
        raise ValueError(self.error)


REPLIES = TypeAdapter(SessionReply.declared_union())


class SessionRequest(TaggedWire, ABC):
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    def exchange(self, claim: WatchClaim) -> None:
        """Control requests never inject an input or retry an emitted event."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect(str(claim.socket_path))
            peer.sendall((self.model_dump_json() + '\n').encode())
            with peer.makefile('rb') as stream:
                REPLIES.validate_json(stream.readline(4096)).require()

    @abstractmethod
    async def handle(self, service: MonitorService, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None: ...


class AttachMonitor(SessionRequest):
    wire_tag = 'attach'

    async def handle(self, service: MonitorService, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        bridge = service.bridge
        attachment = MonitorAttachment.opened(writer, bridge.binding.thread)
        async with bridge.reader_changed:
            if bridge.reader is not None:
                raise ValueError('This session already has a Monitor reader')
            bridge.reader = attachment
        try:
            await attachment.write(SessionReady().model_dump_json())
            await attachment.write(attachment.probe.model_dump_json())
            print('Monitor reader attached; waiting for its fresh probe.', file=sys.stderr, flush=True)
            await reader.read()  # EOF, not a tool timeout, ends this attachment only.
        finally:
            async with bridge.reader_changed:
                bridge.reader = None
                bridge.reader_changed.notify_all()
            await attachment.close()
            print('Monitor reader detached; delivery paused, bridge remains connected.', file=sys.stderr, flush=True)


class VerifySession(SessionRequest):
    wire_tag = 'verify'
    owner: ProcessIdentity

    def require_owner(self, service: MonitorService) -> None:
        if self.owner != service.owner:
            raise ValueError('This bridge belongs to a different Claude process incarnation')

    async def handle(self, service: MonitorService, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        self.require_owner(service)
        writer.write((SessionReady().model_dump_json() + '\n').encode())
        await writer.drain()


class EndSession(VerifySession):
    wire_tag = 'end'

    async def handle(self, service: MonitorService, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        await super().handle(service, reader, writer)
        service.task.cancel()


REQUESTS = TypeAdapter(SessionRequest.declared_union())


@dataclass
class MonitorService:
    """The Claude process, not any reader, owns the registered bridge tasks."""

    bridge: LineMonitorBridge
    owner: ProcessIdentity
    task: asyncio.Task

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = REQUESTS.validate_json(await reader.readline())
            await request.handle(self, reader, writer)
        except (ValueError, OSError) as error:
            if not writer.is_closing():
                writer.write((SessionRefused(error=str(error)).model_dump_json() + '\n').encode())
                try:
                    await writer.drain()
                except ConnectionError:
                    pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def run(self) -> None:
        path = WatchClaim(self.bridge.binding.thread).socket_path
        # Caller already owns the watch claim and registered endpoint.
        path.unlink(missing_ok=True)
        try:
            async with asyncio.TaskGroup() as clients:
                server = await asyncio.start_unix_server(
                    lambda reader, writer: clients.create_task(self.accept(reader, writer)), str(path))
                async with server:
                    print(f'Session bridge ready for Claude PID {self.owner.pid}.', file=sys.stderr, flush=True)
                    try:
                        await self.owner.wait_exited()
                    finally:
                        self.task.cancel()
        finally:
            path.unlink(missing_ok=True)


async def read_monitor(claim: WatchClaim) -> None:
    """This is the complete lifetime of a Monitor tool's command: read only."""
    with claim.reader_held():
        reader, writer = await asyncio.open_unix_connection(str(claim.socket_path))
        try:
            writer.write((AttachMonitor().model_dump_json() + '\n').encode())
            await writer.drain()
            REPLIES.validate_json(await reader.readline()).require()
            async for line in reader:
                # Exactly one stdout line becomes one real Monitor event. No spool.
                sys.stdout.write(line.decode())
                sys.stdout.flush()
        finally:
            writer.close()
            await writer.wait_closed()
