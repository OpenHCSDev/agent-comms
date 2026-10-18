"""Pi JSON-line transport; tolerant discovery and strict native proof share one decoder."""

from __future__ import annotations
from .field_codec import FieldCodec, MalformedRecord
from .jsonl_stream import JsonlStreamReader
from .pi_events import PiEvent, Response
from contextlib import asynccontextmanager

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import uuid4

from .pending_requests import PendingRequests
from .sealed import Sealed

if TYPE_CHECKING:
    from .pi_commands import PiCommand


class PiRpcChannel(JsonlStreamReader, Sealed):
    """Read whole JSONL records regardless of asyncio's transport buffer limit.

    Pi's end-of-turn events may contain many messages in one record. Retain
    consumed fragments across cancellation when the finish signal wins the
    read race, so the next read can finish the same record without losing bytes.
    """

    OBSERVATION_MAX_BYTES = 16 * 1024

    def __init__(self, reader: asyncio.StreamReader, *, event_type: type[PiEvent] = PiEvent):
        super().__init__(reader)
        self.pending = PendingRequests()
        self.event_type = event_type
        self._events = asyncio.Queue(maxsize=100)
        self._read_task: asyncio.Task | None = None

    async def _read(self):
        try:
            while (event := await self.receive(strict=True)) is not None:
                if not isinstance(event, Response) or self.correlate(event) is None:
                    await self.deliver_event(event)
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

    async def request(self, command, writer):
        if self._read_task is None:
            return await command.exchange(self, writer, strict=True, on_event=self.event_type.unowned_event)
        async with command.pending_response(self, writer) as future:
            await asyncio.wait((future, self._read_task), return_when=asyncio.FIRST_COMPLETED)
            response = future.result() if future.done() else self._read_task.result()
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

    def encode(self, command: PiCommand) -> bytes:
        """Register before writing; correlated proof still requires its native input ID."""
        self.track(command)
        return self.command_bytes(command)

    def track(self, command: PiCommand) -> asyncio.Future[Response]:
        """Expose the same pending response to the request's lifecycle owner."""
        key = command.id or uuid4().hex
        return self.pending.add(type(command), key, request=command)

    @staticmethod
    def command_bytes(command: PiCommand) -> bytes:
        return (json.dumps(command.to_rpc()) + "\n").encode()

    def correlate(self, response) -> PiCommand | None:
        owner = response.command
        return (
            self.pending.take(owner, response.id, response)
            if response.id
            else self.pending.take_anonymous(owner, response)
        )
