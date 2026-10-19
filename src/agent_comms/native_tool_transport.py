"""Per-input native socket and call identities, shared by declared tool policies."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
from collections.abc import Callable, ValuesView
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar, TYPE_CHECKING

from .field_codec import MalformedRecord, Token256Text
from .native_tool_call import NativeToolCall, OwnerToolConnection, SelectedToolDenied, DuplicateToolCallIdentity
from .pi_payloads import PiContent, ToolCallContent
from .private_path import PrivateDirectoryRole, PrivateSocketRole

if TYPE_CHECKING:
    from .pi_events import ToolExecutionStart, ToolExecutionEnd

_LOG = logging.getLogger(__name__)

Call = TypeVar("Call", bound=NativeToolCall)


@dataclass(frozen=True, slots=True)
class ToolCallIdentity:
    """A provider call id in the assistant tool round that declared it."""

    round_number: int
    call_id: str


@dataclass(frozen=True)
class NativeToolRound(Generic[Call]):
    """One decoded assistant message; call ids are unique only within this boundary."""

    number: int
    declared: tuple[Call, ...]

    @classmethod
    def decode(
        cls,
        number: int,
        content: tuple[PiContent, ...],
        decode_call: Callable[[object, object, object], Call],
    ) -> NativeToolRound[Call]:
        declared: list[Call] = []
        seen: set[str] = set()
        for item in content:
            if isinstance(item, ToolCallContent):
                call = decode_call(item.id, item.name, item.arguments)
                if call.call_id in seen:
                    raise DuplicateToolCallIdentity(call.call_id)
                seen.add(call.call_id)
                declared.append(call)
            elif not item.tool_round_allowed:
                raise SelectedToolDenied("Invalid native tool content")
        if not declared:
            raise SelectedToolDenied("Native tool round has no declared call")
        return cls(number, tuple(declared))


class NativeToolCallTable(Generic[Call]):
    """Broker-owned identities and lifecycle objects for every native tool round."""

    def __init__(self) -> None:
        # A request has only a call id until the native message declares its
        # round. Keep that unresolved identity in this same table; socket
        # arrival must never assign the next observed round number.
        self._calls: dict[ToolCallIdentity | str, Call] = {}
        self.rounds: list[NativeToolRound[Call]] = []

    def __bool__(self) -> bool:
        return bool(self._calls)

    def decode_round(
        self,
        content: tuple[PiContent, ...],
        decode_call: Callable[[object, object, object], Call],
    ) -> NativeToolRound[Call]:
        round = NativeToolRound.decode(len(self.rounds), content, decode_call)
        if self.rounds and not all(call.complete for call in self.rounds[-1].declared):
            raise SelectedToolDenied(
                "Native tool round was announced again before its calls completed"
            )
        return round

    def announce(self, round: NativeToolRound[Call]) -> None:
        if round.number != len(self.rounds):
            raise SelectedToolDenied("Native tool round sequence changed")
        identities = tuple(ToolCallIdentity(round.number, call.call_id) for call in round.declared)
        resolved: list[Call] = []
        for identity, candidate in zip(identities, round.declared, strict=True):
            existing = self._calls.get(identity.call_id)
            if existing is not None:
                existing.correlate(candidate)
                resolved.append(existing)
            else:
                resolved.append(candidate)
        announced = NativeToolRound(round.number, tuple(resolved))
        for call in announced.declared:
            call.announce()
        for identity, call in zip(identities, resolved, strict=True):
            self._calls.pop(identity.call_id, None)
            self._calls[identity] = call
        self.rounds.append(announced)

    def call_for(self, candidate: Call) -> Call:
        if self.rounds:
            current_identity = ToolCallIdentity(self.rounds[-1].number, candidate.call_id)
            current = self._calls.get(current_identity)
            if current is not None and current.accepts_current_request(candidate):
                return current
        existing = self._calls.get(candidate.call_id)
        if existing is not None:
            existing.correlate(candidate)
            return existing
        self._calls[candidate.call_id] = candidate
        return candidate

    def identity_for(self, call: Call) -> ToolCallIdentity:
        """Check permission only after this exact declared call has started."""
        identity, current = self.require_current(
            call.call_id, "Native tool request has no declared round"
        )
        if current is not call:
            raise SelectedToolDenied("Native tool request belongs to another round")
        return identity

    def require_current(
        self, call_id: str, missing_message: str
    ) -> tuple[ToolCallIdentity, Call]:
        if self.rounds:
            identity = ToolCallIdentity(self.rounds[-1].number, call_id)
            call = self._calls.get(identity)
            if call is not None:
                return identity, call
        raise SelectedToolDenied(missing_message)

    def require(self, round_number: int, call_id: str) -> Call:
        return self._calls[ToolCallIdentity(round_number, call_id)]

    def values(self) -> ValuesView[Call]:
        return self._calls.values()



class ToolTransport(Generic[Call]):
    """Authenticated native child transport; the policy decides which requests may run."""

    max_request = 1024 * 1024

    @property
    def connection(self) -> OwnerToolConnection:
        return OwnerToolConnection(str(self.address), self.token)

    @property
    def retainable(self) -> bool:
        return True

    @classmethod
    async def open(cls, turn, ownership, *policy):
        transport = cls(turn.launch.session.directory, os.urandom(32).hex(), *policy)
        transport.turn = turn
        ownership.push_async_callback(transport.close)
        await transport.start()
        return transport

    def bind_process(self, pid: int) -> None:
        self.expected_pid = pid

    def __init__(self, directory: Path, token: str) -> None:
        try:
            Token256Text.decode(token)
        except MalformedRecord as error:
            raise ValueError("Selected tool transport requires a random 256-bit token") from error
        self.path = Path(directory).absolute() / "s"
        self._resources = ExitStack()
        self.token = token
        self.expected_pid: int | None = None
        # Live owner-side completion only. A visible .done file after failed
        # fsync is not a durable acknowledgement and must never be promoted by itself.
        self._server: asyncio.AbstractServer | None = None
        self._created = False
        self.calls: NativeToolCallTable[Call] = NativeToolCallTable()
        self._handlers: set[asyncio.Task] = set()

    def decode_call(self, call_id: object, name: object, arguments: object) -> Call:
        raise SelectedToolDenied("Native tools are disabled for this input")

    def decode_request(self, raw: bytes) -> Call:
        raise SelectedToolDenied("Native tools are disabled for this input")

    async def allow(self, round_number: int, call: Call) -> None:
        raise SelectedToolDenied("Native tools are disabled for this input")

    def validate_round(self, round: NativeToolRound[Call]) -> None:
        """Apply policy-specific limits after full decode and before any state transition."""

    def announce(self, content: tuple[PiContent, ...]) -> None:
        round = self.calls.decode_round(content, self.decode_call)
        self.validate_round(round)
        self.calls.announce(round)

    def tool_started(self, event: ToolExecutionStart) -> None:
        candidate = self.decode_call(event.tool_call_id, event.tool_name, event.args)
        _, call = self.calls.require_current(
            candidate.call_id, "Native tool start has no declared call"
        )
        call.correlate(candidate)
        call.start()

    def tool_finished(self, event: ToolExecutionEnd, input_id: str) -> None:
        identity, call = self.calls.require_current(
            event.tool_call_id, "Native tool terminal has no declared call"
        )
        call.finish(
            event.tool_name, event.is_error, self.path.parent, input_id,
            identity.round_number,
        )

    def assert_complete(self) -> None:
        for call in self.calls.values():
            call.assert_complete()

    @property
    def active_tools(self) -> set[str]:
        return {
            call.call_id for call in self.calls.values() if call.observation.executing
        }

    async def handle_request(self, raw: bytes) -> dict[str, object]:
        call = self.calls.call_for(self.decode_request(raw))
        await call.authorize(lambda: self.allow(self.calls.identity_for(call).round_number, call))
        return {"ok": True}

    def failure_response(self, error: Exception) -> dict[str, object]:
        return {"ok": False, "error": "Selected tool denied or outcome UNKNOWN; no retry"}

    async def start(self) -> None:
        if not hasattr(socket, "SO_PEERCRED") or not hasattr(os, "O_NOFOLLOW"):
            raise SelectedToolDenied("Selected tool requires a peer-credential Unix socket")
        info = self.path.parent.lstat()
        if PrivateDirectoryRole.violation(info) is not None or self.path.parent.resolve() != self.path.parent:
            raise SelectedToolDenied("Selected tool socket parent is not private")
        if self.path.exists() or self.path.is_symlink():
            raise SelectedToolDenied("Selected tool socket already exists")
        try:
            self.address = self._resources.enter_context(PrivateSocketRole.address(self.path))
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(self.address), limit=self.max_request + 1
            )
            self._created = True
            os.chmod(self.path, 0o600)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        # Python's server wait_closed can wait for existing client transports.
        # Cancel/drain those handlers before awaiting listener completion.
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        if self._created:
            self.path.unlink(missing_ok=True)
            self._created = False
        self._resources.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            try:
                if self._server is None:
                    raise SelectedToolDenied("Owner tool socket is closed")
                sock = writer.get_extra_info("socket")
                if sock is None or self.expected_pid is None:
                    raise SelectedToolDenied("Selected tool child is not bound")
                pid, uid, _gid = struct.unpack(
                    "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if pid != self.expected_pid or uid != os.geteuid():
                    raise SelectedToolDenied("Selected tool peer is not the launched Pi child")
                raw = await asyncio.wait_for(reader.readline(), timeout=10)
                response = await self.handle_request(raw)
                answer = "allowed"
            except (ValueError, TimeoutError) as error:
                # SelectedToolDenied (a ValueError) is every typed denial and
                # UNKNOWN outcome; ValueError also covers the untrusted child's
                # undecodable request. Any other failure is a defect: it
                # propagates and the child sees a closed socket, never a reply.
                response = self.failure_response(error)
                answer = "refused"
            try:
                writer.write((json.dumps(response) + "\n").encode("ascii"))
                await writer.drain()
            except ConnectionError:
                # Not an owner failure: the child gave up and told the model
                # so. Whatever the owner did for this call stays recorded.
                _LOG.warning(
                    "The Pi child stopped waiting for a tool check before its answer "
                    "(%s) could be sent", answer,
                )
        finally:
            writer.close()
            try:
                with suppress(ConnectionError):
                    await writer.wait_closed()
            finally:
                self._handlers.discard(task)
