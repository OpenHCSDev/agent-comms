"""Per-input native socket and call identities, shared by declared tool policies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from .child_process import join_retirement
from .field_codec import FieldCodec, MalformedRecord, Token256Text
from .native_tool_call import (
    DuplicateToolCallIdentity,
    NativeToolCall,
    OwnerToolConnection,
    SelectedToolDenied,
)
from .pi_payloads import PiContent, ToolCallContent
from .private_path import PrivateDirectoryRole, PrivateSocketRole

if TYPE_CHECKING:
    from .pi_events import ToolExecutionEnd, ToolExecutionStart

_LOG = logging.getLogger(__name__)

Call = TypeVar("Call", bound=NativeToolCall)


class ToolConnectionOutcome(ABC):
    """One response attempt decides whether the owner still has a writer to close."""

    @classmethod
    async def send(
        cls, writer: asyncio.StreamWriter, response: dict[str, object], answer: str
    ) -> ToolConnectionOutcome:
        try:
            writer.write((json.dumps(response) + "\n").encode("ascii"))
            await writer.drain()
        except ConnectionError:
            return ToolChildGone(answer)
        return ToolAnswerSent()

    @abstractmethod
    def close(self, ownership: ExitStack) -> None: ...


class ToolAnswerSent(ToolConnectionOutcome):
    def close(self, ownership: ExitStack) -> None:
        ownership.close()


@dataclass(frozen=True)
class ToolChildGone(ToolConnectionOutcome):
    answer: str

    def close(self, ownership: ExitStack) -> None:
        # asyncio has already closed the failed transport. Do not close it or
        # await its failed close future a second time.
        ownership.pop_all()
        _LOG.warning(
            "The Pi child stopped waiting for a tool check before its answer " "(%s) could be sent",
            self.answer,
        )


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


class NativeToolCallTable(dict[ToolCallIdentity | str, Call]):
    """Broker-owned identities and lifecycle objects for every native tool round."""

    def __init__(self) -> None:
        # A request has only a call id until the native message declares its
        # round. Keep that unresolved identity in this same table; socket
        # arrival must never assign the next observed round number.
        super().__init__()
        self.rounds: list[NativeToolRound[Call]] = []

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
            existing = self.get(identity.call_id)
            if existing is not None:
                existing.correlate(candidate)
                resolved.append(existing)
            else:
                resolved.append(candidate)
        announced = NativeToolRound(round.number, tuple(resolved))
        for call in announced.declared:
            call.announce()
        for identity, call in zip(identities, resolved, strict=True):
            self.pop(identity.call_id, None)
            self[identity] = call
        self.rounds.append(announced)

    def call_for(self, candidate: Call) -> Call:
        if self.rounds:
            current_identity = ToolCallIdentity(self.rounds[-1].number, candidate.call_id)
            current = self.get(current_identity)
            if current is not None and current.accepts_current_request(candidate):
                return current
        existing = self.get(candidate.call_id)
        if existing is not None:
            existing.correlate(candidate)
            return existing
        self[candidate.call_id] = candidate
        return candidate

    def identity_for(self, call: Call) -> ToolCallIdentity:
        """Check permission only after this exact declared call has started."""
        identity, current = self.require_current(
            call.call_id, "Native tool request has no declared round"
        )
        if current is not call:
            raise SelectedToolDenied("Native tool request belongs to another round")
        return identity

    def require_current(self, call_id: str, missing_message: str) -> tuple[ToolCallIdentity, Call]:
        if self.rounds:
            identity = ToolCallIdentity(self.rounds[-1].number, call_id)
            call = self.get(identity)
            if call is not None:
                return identity, call
        raise SelectedToolDenied(missing_message)

    def require(self, round_number: int, call_id: str) -> Call:
        return self[ToolCallIdentity(round_number, call_id)]


class ToolListener:
    """One private endpoint and joined resource lifetime, independent of child binding."""

    def __init__(self, directory: Path, token: str) -> None:
        try:
            Token256Text.decode(token)
        except MalformedRecord as error:
            raise ValueError("Selected tool transport requires a random 256-bit token") from error
        self.session_directory = Path(directory).absolute()
        self.token = token
        self._resources = AsyncExitStack()
        if not hasattr(socket, "SO_PEERCRED") or not hasattr(os, "O_NOFOLLOW"):
            raise SelectedToolDenied("Selected tool requires a peer-credential Unix socket")
        PrivateDirectoryRole.require(self.session_directory.lstat())
        if self.session_directory.resolve() != self.session_directory:
            raise SelectedToolDenied("Selected tool socket parent is not private")
        with ExitStack() as prepared:
            directory = self.session_directory / self.token[:32]
            os.mkdir(directory, 0o700)
            prepared.callback(directory.rmdir)
            self.path = directory / "s"
            self.address = prepared.enter_context(PrivateSocketRole.address(self.path))
            self.socket = prepared.enter_context(socket.socket(socket.AF_UNIX))
            self.socket.bind(str(self.address))
            prepared.callback(self.path.unlink, missing_ok=True)
            os.chmod(self.path, 0o600)
            self._resources.enter_context(prepared.pop_all())

    @property
    def connection(self) -> OwnerToolConnection:
        return OwnerToolConnection(str(self.address), self.token)

    async def listen(self, transport: ToolTransport) -> None:
        try:
            self.server = await asyncio.start_unix_server(
                transport._handle,
                sock=self.socket,
                limit=transport.max_request + 1,
                start_serving=False,
                cleanup_socket=False,
            )
            self._resources.push_async_callback(transport._drain_calls)
            self._resources.push_async_callback(self.server.wait_closed)
            self._resources.push_async_callback(transport._drain_handlers)
            self._resources.callback(self.server.close)
            await self.server.start_serving()
        except BaseException:
            await self.close()
            raise

    @cached_property
    def _retirement(self) -> asyncio.Task[None]:
        return asyncio.create_task(self._resources.aclose())

    async def close(self) -> None:
        await join_retirement(self._retirement)


@dataclass(frozen=True)
class UnboundToolTransport(Generic[Call]):
    """A prepared endpoint cannot accept requests until its actual child exists."""

    listener: ToolListener
    bind: Callable[[int], ToolTransport[Call]]

    async def bind_process(self, pid: int) -> ToolTransport[Call]:
        transport = self.bind(pid)
        await transport.start()
        return transport


class ToolTransport(Generic[Call]):
    """A child-bound request policy. An instance always has its authenticated PID."""

    max_request = 1024 * 1024

    @property
    def retainable(self) -> bool:
        return True

    @classmethod
    async def open(cls, turn, ownership, *policy) -> UnboundToolTransport:
        listener = ToolListener(turn.launch.session.directory, os.urandom(32).hex())
        ownership.push_async_callback(listener.close)

        def bind(pid: int):
            transport = cls(listener, *policy, pid=pid)
            transport.turn = turn
            return transport

        return UnboundToolTransport(listener, bind)

    def __init__(self, listener: ToolListener, *, pid: int) -> None:
        self.listener = listener
        self.expected_pid = pid
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
        # The call decoders validate the call's fields as Pi sent them.
        candidate = self.decode_call(
            FieldCodec.encode(event.tool_call_id), FieldCodec.encode(event.tool_name), event.args
        )
        _, call = self.calls.require_current(
            candidate.call_id, "Native tool start has no declared call"
        )
        call.correlate(candidate)
        call.start()

    def tool_finished(self, event: ToolExecutionEnd, input_id: str) -> None:
        identity, call = self.calls.require_current(
            FieldCodec.encode(event.tool_call_id), "Native tool terminal has no declared call"
        )
        call.finish(
            FieldCodec.encode(event.tool_name),
            FieldCodec.encode(event.is_error),
            self.listener.session_directory,
            input_id,
            identity.round_number,
        )

    def assert_complete(self) -> None:
        for call in self.calls.values():
            call.assert_complete()

    async def handle_request(self, raw: bytes) -> dict[str, object]:
        call = self.calls.call_for(self.decode_request(raw))
        await call.authorize(lambda: self.allow(self.calls.identity_for(call).round_number, call))
        return {"ok": True}

    @staticmethod
    def failure_response(error: Exception) -> dict[str, object]:
        return {"ok": False, "error": "Selected tool denied or outcome UNKNOWN; no retry"}

    async def start(self) -> None:
        await self.listener.listen(self)

    async def close(self) -> None:
        await self.listener.close()

    async def _drain_calls(self) -> None:
        """Retire policy-owned call resources after handler and server retirement."""

    async def _drain_handlers(self) -> None:
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            with ExitStack() as connection:
                connection.callback(writer.close)
                outcome = await self._reply(reader, writer)
                outcome.close(connection)
        finally:
            self._handlers.discard(task)

    async def _reply(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> ToolConnectionOutcome:
        try:
            if not self.listener.server.is_serving():
                raise SelectedToolDenied("Owner tool socket is closed")
            sock = writer.get_extra_info("socket")
            pid, uid, _gid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if pid != self.expected_pid or uid != os.geteuid():
                raise SelectedToolDenied("Selected tool peer is not the launched Pi child")
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=10)
            except ConnectionError:
                return ToolChildGone("unanswered")
            response = await self.handle_request(raw)
            answer = "allowed" if response.get("ok") is True else "refused"
        except (SelectedToolDenied, MalformedRecord) as error:
            # Only declared denials and malformed child input become a refusal.
            # Owner defects propagate; they never become a successful answer.
            response = self.failure_response(error)
            answer = "refused"
        return await ToolConnectionOutcome.send(writer, response, answer)
