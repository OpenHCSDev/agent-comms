"""Pi JSON-line transport; tolerant discovery and strict native proof share one decoder."""

from __future__ import annotations
from .jsonl_stream import JsonlStreamReader

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .pending_requests import PendingRequests
from .sealed import Sealed

if TYPE_CHECKING:
    from .pi_commands import PiCommand
    from .pi_events import PiEvent, Response


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Pi RPC field")
        result[key] = value
    return result


class PiRpcChannel(JsonlStreamReader, Sealed):
    """Read whole JSONL records regardless of asyncio's transport buffer limit.

    Pi's end-of-turn events may contain many messages in one record. Retain
    consumed fragments across cancellation when the finish signal wins the
    read race, so the next read can finish the same record without losing bytes.
    """

    OBSERVATION_MAX_BYTES = 16 * 1024

    def __init__(self, reader: asyncio.StreamReader):
        super().__init__(reader)
        self.pending = PendingRequests()

    @staticmethod
    def decode_record(raw: bytes, *, strict: bool = False, max_bytes: int | None = None) -> PiEvent:
        from .pi_events import PiEvent

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
