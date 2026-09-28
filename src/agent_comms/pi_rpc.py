"""Pi JSON-line transport; tolerant discovery and strict native proof share one decoder."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from .pending_requests import PendingRequests
from .pi_commands import PiCommand
from .pi_events import PiEvent


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Pi RPC field")
        result[key] = value
    return result


class PiRpcChannel:
    """Read whole JSONL records regardless of asyncio's transport buffer limit.

    Pi's end-of-turn events may contain many messages in one record. Retain
    consumed fragments across cancellation when the finish signal wins the
    read race, so the next read can finish the same record without losing bytes.
    """

    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader
        self.pending = PendingRequests()
        self.chunks: list[bytes] = []

    async def readline(self, *, max_bytes: int | None = None) -> bytes:
        size = sum(map(len, self.chunks))
        while True:
            try:
                line = await self.reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as error:
                if max_bytes is not None and size + error.consumed > max_bytes:
                    self.chunks.clear()
                    raise ValueError("Native RPC record exceeds transport limit") from error
                self.chunks.append(await self.reader.readexactly(error.consumed))
                size += error.consumed
                continue
            except asyncio.IncompleteReadError as error:
                line = error.partial
            if max_bytes is not None and size + len(line) > max_bytes:
                self.chunks.clear()
                raise ValueError("Native RPC record exceeds transport limit")
            self.chunks.append(line)
            record = b"".join(self.chunks)
            self.chunks.clear()
            return record

    @staticmethod
    def decode_record(raw: bytes, *, strict: bool = False, max_bytes: int | None = None) -> PiEvent:
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError("Native RPC record exceeds transport limit")
        if strict and (not raw or not raw.endswith(b"\n")):
            raise ValueError("Native RPC record is incomplete")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields if strict else None)
        if not isinstance(data, dict):
            raise ValueError("Pi RPC record has wrong type")
        return PiEvent.from_wire(data)

    async def receive(
        self, *, strict: bool = False, max_bytes: int | None = None
    ) -> PiEvent | None:
        raw = await self.readline(max_bytes=max_bytes)
        if not raw:
            return None
        return self.decode_record(raw, strict=strict, max_bytes=max_bytes)

    def encode(self, command: PiCommand) -> bytes:
        """Register before writing; correlated proof still requires its native input ID."""
        key = command.id or uuid4().hex
        self.pending.add(type(command), key, request=command)
        return self.command_bytes(command)

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
