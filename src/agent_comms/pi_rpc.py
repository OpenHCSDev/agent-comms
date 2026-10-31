"""Pi JSON-line transport; tolerant discovery and strict native proof share one decoder."""

from __future__ import annotations
from .nominal.field_codec import FieldCodec, MalformedRecord
from .jsonl_stream import JsonlStreamReader
from .pi_events import PiEvent, Response
from contextlib import asynccontextmanager

import asyncio
import json
from typing import TYPE_CHECKING

from .pending_requests import PendingRequest, PendingRequests
from .nominal.sealed import Sealed

if TYPE_CHECKING:
    from .pi_commands import PiCommand


class PiRpcChannel(JsonlStreamReader, Sealed):
    """Read whole JSONL records regardless of asyncio's transport buffer limit.

    Pi's end-of-turn events may contain many messages in one record. Retain
    consumed fragments across cancellation when the finish signal wins the
    read race, so the next read can finish the same record without losing bytes.
    """

    OBSERVATION_MAX_BYTES = 16 * 1024
    DRAIN_CHUNK_BYTES = 64 * 1024

    def __init__(self, reader: asyncio.StreamReader, *, event_type: type[PiEvent] = PiEvent):
        # Node writes to a stdout pipe synchronously on Linux: a pipe Core stops
        # draining freezes Pi's whole event loop, model stream included. Records
        # are read from an unpaused copy that one task keeps filling from the
        # child's pipe, so draining never waits on how Core handles a record.
        super().__init__(asyncio.StreamReader())
        self.source = reader
        self.draining = asyncio.get_running_loop().create_task(self._drain_source())
        self.pending = PendingRequests()
        self.event_type = event_type
        self._events = asyncio.Queue(maxsize=100)
        self._read_task: asyncio.Task | None = None

    async def _drain_source(self) -> None:
        try:
            while chunk := await self.source.read(self.DRAIN_CHUNK_BYTES):
                self.reader.feed_data(chunk)
        except OSError as error:
            # The pipe's own failure reaches the record reader, as it did before.
            self.reader.set_exception(error)
            return
        self.reader.feed_eof()

    async def _read(self):
        try:
            while (event := await self.receive(strict=True)) is not None:
                await event.deliver_to(self)
        finally:
            self._events.shutdown()

    async def deliver_event(self, event):
        await self._events.put(event)

    @asynccontextmanager
    async def turn(self):
        if self._read_task is not None:
            raise RuntimeError('A Pi turn is already open')
        self._events = asyncio.Queue(maxsize=100)
        self._read_task = asyncio.create_task(self._read())
        try:
            yield self
        finally:
            await self.finish_input()

    async def finish_input(self):
        reading, self._read_task = self._read_task, None
        if reading is not None:
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
        self.pending.cancel_all()

    async def next_event(self):
        reading = self._read_task
        if reading is None:
            raise RuntimeError('No Pi turn is open')
        try:
            return await self._events.get()
        except asyncio.QueueShutDown:
            return reading.result()

    async def request(self, command, child):
        if self._read_task is None:
            return await command.exchange(child, strict=True, on_event=self.event_type.unowned_event)
        pending = await child.send_request(command)
        await asyncio.wait((pending.future, self._read_task), return_when=asyncio.FIRST_COMPLETED)
        response = pending.future.result() if pending.future.done() else self._read_task.result()
        if response is None:
            raise EOFError('Pi child ended before its response')
        return response

    async def converted(self):
        while (event := await self.next_event()) is not None:
            for update in event.convert():
                yield update

    @staticmethod
    def decode_record(raw: bytes, *, strict: bool = False, max_bytes: int | None = None, event_type: type[PiEvent] = PiEvent) -> PiEvent:
        if max_bytes is not None and len(raw) > max_bytes:
            raise MalformedRecord("Native RPC record exceeds transport limit")
        if strict and (not raw or not raw.endswith(b"\n")):
            raise MalformedRecord("Native RPC record is incomplete")
        # Discovery skips lines that are not JSON; the native proof refuses them.
        data = FieldCodec.parse(raw) if strict else json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise MalformedRecord("Pi RPC record has wrong type")
        return event_type.from_wire(data)

    async def receive(
        self, *, strict: bool = False, max_bytes: int | None = None
    ) -> PiEvent | None:
        raw = await self.readline(max_bytes=max_bytes)
        if not raw:
            return None
        return self.decode_record(raw, strict=strict, max_bytes=max_bytes, event_type=self.event_type)

    def track[Command: PiCommand](self, command: Command) -> PendingRequest[Command]:
        """Share the original request and reply with its lifecycle owner."""
        return command.id.track_in(self.pending, command.response_type(), command)

    @staticmethod
    def command_bytes(command: PiCommand) -> bytes:
        return (json.dumps(command.to_rpc()) + "\n").encode()

    def correlate(self, response: Response) -> PiCommand:
        command = response.id.take_from(self.pending, response.command, response)
        if command is None:
            raise MalformedRecord("Pi RPC response has no pending command")
        return command
