"""Default-off, one-slot owner boundary for a selected Pi tool request.

The model supplies only a relative existing-file resource and UTF-8 replacement
text. The owner supplies every admission field; neither an injected prompt nor
a socket message can mint a wake admission. Consumption is durable before any
claim append or file mutation, and UNKNOWN is never retried automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import stat
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable, ValuesView
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Generic, TypeVar

from agent_comms.coordinator import Coordination

from .claim_admission import publish_selected_resource_claim, write_selected_claimed_file
from .comms import Comms
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import CoordinationReadUnavailable
from .coordination_tables.participants import OwnerGenerations
from .envelope_claim_transitions import ExistingFileClaim, WakeAdmission
from .field_codec import FieldCodec
from .native_input_record import FullNativeExecution, NativeInputIdentity, NativeInputIdText
from .native_runtime_input import NativeRuntimeInput
from .native_tool_call import (
    CoordinationBusy,
    DuplicateToolCallIdentity,
    NativeToolCall,
    SelectedToolDenied,
)
from .pi_events import ToolExecutionEnd, ToolExecutionStart
from .pi_payloads import PiContent, ToolCallContent
from .pi_rpc import unique_fields
from .private_path import PrivateDirectoryRole, PrivateFileRole, PrivateSocketRole
from .selected_actions import SelectedAction

# Stay well below the existing native RPC record cap (1 MiB, including JSON).
_MAX_CONTENT = 128 * 1024
_MAX_REQUEST = _MAX_CONTENT + 8192
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_TOOL_SOURCE_SHA = "136695f78d636886725c22c1bd7373d1f18d1b6001f4192d3afe9ec3a1ba102b"

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SelectedToolIntent(SelectedAction):
    """Trusted, explicit pre-turn opt-in; no tool call or write authority.

    The owner must create a distinct bound SelectedToolMode only after reserving
    the exact FULL input. Do not construct this from injected/model text.
    """

    instruction = (
        "Answer the original committed message directly and concisely. "
        "You may call selected_claimed_write at most once to request a complete UTF-8 "
        "replacement of an existing worktree file (maximum 128 KiB); the owner "
        "independently checks the active selected wake, claim and write before the tool "
        "returns. Tool failure/UNKNOWN must not be retried. "
        "No shell, generic edits or other tools. "
    )

    def mode(self, owner):
        return selected_tool_mode_for_owner(
            owner.comms,
            owner.store,
            owner.admission,
            owner.owner_name,
            owner.session_dir,
            owner.input_id,
        )


class NativeToolMode(ABC):
    """Owner-bound native tool policy shared by selected and normal coding turns."""

    @abstractmethod
    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        """Return the native tool selection and reviewed extension."""

    @abstractmethod
    def socket(self, directory: Path, token: str) -> OwnerToolSocket:
        """Create the existing authenticated owner transport for this policy."""


@dataclass(frozen=True, slots=True)
class SelectedToolMode(NativeToolMode):
    """Trusted owner callback, never parsed from a model call or injected text."""

    action: Callable[[SelectedToolRequest], None]

    def __post_init__(self) -> None:
        if not callable(self.action):
            raise TypeError("Selected tool mode requires an owner callback")

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        return (
            "--no-builtin-tools",
            "--tools",
            "selected_claimed_write",
            "-e",
            str(selected_extension(package)),
        )

    def socket(self, directory: Path, token: str) -> OwnerToolSocket:
        return SelectedToolSocket(directory, token, self.action)


@dataclass(frozen=True)
class SelectedWriteArguments:
    """Model-supplied text and relative resource, validated once at ingress."""

    resource: str
    contents: str
    claim: ExistingFileClaim = field(init=False, compare=False)

    def __post_init__(self) -> None:
        if not self.resource or len(self.resource.encode("utf-8")) > 4096:
            raise SelectedToolDenied("Selected tool resource is not bounded")
        path = Path(self.resource)
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or self.resource.startswith(("@", "./"))
        ):
            raise SelectedToolDenied("Selected tool resource must be relative without aliases")
        object.__setattr__(self, "claim", ExistingFileClaim(path))
        if len(self.contents.encode("utf-8", errors="strict")) > _MAX_CONTENT:
            raise SelectedToolDenied("Selected tool contents exceed 128 KiB")


@dataclass
class SelectedToolRequest(NativeToolCall):
    arguments: SelectedWriteArguments

    def __post_init__(self) -> None:
        if not _CALL_ID.fullmatch(self.call_id):
            raise SelectedToolDenied("Selected tool call identity is invalid")

    @property
    def name(self) -> str:
        return "selected_claimed_write"

    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None:
        if is_error:
            raise SelectedToolDenied("Native Pi selected tool did not finish successfully")
        verify_selected_terminal(directory, input_id, self.call_id)

    def admission_failed(self) -> None:
        raise SelectedToolDenied("Native Pi selected tool admission failed; outcome UNKNOWN")


@dataclass(frozen=True)
class SelectedToolEnvelope:
    token: str
    request: SelectedToolRequest

    def authenticate(self, expected_token: str) -> SelectedToolRequest:
        if self.token != expected_token:
            raise SelectedToolDenied("Selected tool transport is not authenticated")
        return self.request


def selected_extension(package: Path) -> Path:
    """Select the tool shipped inside the already verified native package.

    The native deployment manifest admits this exact module. A private session
    directory is state storage, not an extension installation root.
    """
    source = Path(package) / "dist/selected_claimed_write.mjs"
    info = source.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or hashlib.sha256(source.read_bytes()).hexdigest() != _TOOL_SOURCE_SHA
    ):
        raise SelectedToolDenied("Selected native extension differs from reviewed source")
    return source


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def consume_selected_slot(directory: Path, input_id: str, call_id: str) -> None:
    """Persist one consumed slot BEFORE a bus append; even failed fsync is UNKNOWN.

    The caller must provide a private, physical owner-owned session directory.
    An existing slot is never reopened or reclaimed, even after a process crash.
    """
    try:
        NativeInputIdText.decode(input_id)
    except (ValueError, TypeError) as error:
        raise SelectedToolDenied("Selected input identity is invalid") from error
    if type(call_id) is not str or not _CALL_ID.fullmatch(call_id):
        raise SelectedToolDenied("Selected tool call identity is invalid")
    directory = Path(directory).absolute()
    info = directory.lstat()
    if PrivateDirectoryRole.violation(info) is not None or directory.resolve() != directory:
        raise SelectedToolDenied("Selected tool session directory is not private")
    ledger = directory / "selected-tool-ledger"
    try:
        ledger.mkdir(mode=0o700, exist_ok=True)
        info = ledger.lstat()
        if PrivateDirectoryRole.violation(info) is not None:
            raise SelectedToolDenied("Selected tool ledger is not private")
        _sync_dir(directory)
        fd = os.open(ledger / input_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise SelectedToolDenied(
            "Selected tool input already consumed; outcome may be UNKNOWN"
        ) from error
    except OSError as error:
        raise SelectedToolDenied("Selected tool ledger unavailable; outcome UNKNOWN") from error
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((call_id + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        _sync_dir(ledger)
    except OSError as error:
        # Do NOT remove the visible slot; a failed fsync or a crash may have
        # committed it. No automatic re-append, retry, or write follows.
        raise SelectedToolDenied("Selected tool consumption UNKNOWN; do not retry") from error


def record_selected_terminal(directory: Path, input_id: str, call_id: str) -> None:
    """Only a synced terminal receipt lets the tool return success."""
    result = Path(directory).absolute() / "selected-tool-ledger" / (input_id + ".done")
    try:
        descriptor = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write((call_id + "\n").encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        _sync_dir(result.parent)
    except OSError as error:
        raise SelectedToolDenied("Selected tool terminal receipt UNKNOWN; no retry") from error


def selected_tool_mode_for_owner(
    comms: Comms,
    store: Coordination,
    admission: WakeAdmission,
    owner_name: str,
    session_dir: Path,
    input_id: str,
) -> SelectedToolMode:
    """Capture only owner-derived typed fields for this one FULL selected input.

    Construct after the owner has engaged the selected attempt and reserved its
    tracked input. Never derive this mode from user/model/wake-injected text.
    """
    try:
        NativeInputIdText.decode(input_id)
        FieldCodec.decode(str, owner_name)
    except (ValueError, TypeError) as error:
        raise SelectedToolDenied("Selected mode has no typed owner attempt") from error

    path = store.session.path

    def owner_action(request: SelectedToolRequest) -> None:
        # The prompt-send boundary commits this epoch to the exact reserved
        # FULL input before Pi can receive it. A socket token, PID, model text,
        # or context journal alone does not establish this source binding.
        with Coordination(path) as worker_store:
            verify_sent_full_input(worker_store, admission, owner_name, input_id)
            perform_selected_write(
                comms, worker_store, admission, owner_name, session_dir, input_id, request
            )

    return SelectedToolMode(owner_action)


def verify_sent_full_input(
    store: Coordination, admission: WakeAdmission, owner_name: str, input_id: str
) -> None:
    """Only the exact FULL input admitted by the owner may call native tools.

    This is the first check of a tool call and it only reads. Other owners
    write the same database, so the read waits the coordinator's bounded
    time; a database still busy after that is reported as busy, not as a
    refusal of the call and not as an unknown outcome.
    """
    try:
        with Coordination(str(store.session.path)) as scoped, scoped.session.read():
            _require_sent_full_input(scoped, admission, owner_name, input_id)
    except sqlite3.OperationalError as error:
        try:
            CoordinationReadUnavailable.raise_from(error)
        except CoordinationReadUnavailable as unavailable:
            raise CoordinationBusy() from unavailable


def _require_sent_full_input(
    scoped: Coordination, admission: WakeAdmission, owner_name: str, input_id: str
) -> None:
    assert_native_runtime_schema(scoped.session._connection)
    row = NativeRuntimeInput.one(scoped.session._connection, input_id=input_id)
    if row is None:
        raise SelectedToolDenied("Selected tool has no reserved FULL input")
    if admission.wake_assignment_id not in row.execution.source_assignment_ids(
        scoped.session._connection, input_id
    ):
        raise SelectedToolDenied("Selected tool source was not included in this native input")
    expected = NativeInputIdentity(
        input_id, FullNativeExecution(admission.execution_id, admission.attempt_ordinal),
        OwnerGenerations(owner_lookup=admission.recipient_lookup, owner_thread=owner_name, generation=admission.participant_generation),
    )
    if row.identity != expected:
        raise SelectedToolDenied("Selected tool does not match the exact sent FULL input")
    if not row.sent_owner_admission_generation.matches(admission.owner_admission_generation):
        raise SelectedToolDenied("Selected tool names another sending admission")
    if row.reference.recorded or row.verdict is not None:
        raise SelectedToolDenied("Selected input was already settled")


def verify_selected_terminal(directory: Path, input_id: str, call_id: str) -> None:
    """Corroborate a live owner result; a persisted receipt alone grants nothing."""
    try:
        NativeInputIdText.decode(input_id)
    except (ValueError, TypeError) as error:
        raise SelectedToolDenied("Selected input identity is invalid") from error
    receipt = Path(directory).absolute() / "selected-tool-ledger" / (input_id + ".done")
    info = receipt.lstat()
    if PrivateFileRole.violation(info) is not None:
        raise SelectedToolDenied("Selected tool receipt is not private")
    if receipt.read_bytes() != (call_id + "\n").encode("ascii"):
        raise SelectedToolDenied("Selected tool has no matching terminal receipt")


def perform_selected_write(
    comms: Comms,
    store: Coordination,
    admission: WakeAdmission,
    owner_name: str,
    session_dir: Path,
    input_id: str,
    request: SelectedToolRequest,
) -> None:
    """Only the owner calls the existing selected claim and writer boundaries."""
    if type(request) is not SelectedToolRequest or type(admission) is not WakeAdmission:
        raise SelectedToolDenied("Selected tool has no typed owner admission")
    consume_selected_slot(session_dir, input_id, request.call_id)
    # Any exception from here leaves the slot consumed. The caller reports
    # UNKNOWN and never automatically reissues the claim or mutation.
    claimed = publish_selected_resource_claim(
        comms, store, admission, owner_name, request.arguments.claim
    )
    write_selected_claimed_file(
        comms, store, admission, owner_name, claimed, request.arguments.contents.encode("utf-8")
    )
    record_selected_terminal(session_dir, input_id, request.call_id)


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
        """Resolve admission only after this exact declared call has started."""
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

    @property
    def first_call_id(self) -> str | None:
        """The selected call, or None when a selected-mode turn used no tool."""
        if not self.rounds:
            return None
        return self.rounds[0].declared[0].call_id


class OwnerToolSocket(ABC, Generic[Call]):
    """Authenticated native child transport; the policy owns request admission."""

    max_request = _MAX_REQUEST

    def __init__(self, directory: Path, token: str) -> None:
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise ValueError("Selected tool transport requires a random 256-bit token")
        self.path = Path(directory).absolute() / "s"
        self._resources = ExitStack()
        self.token = token
        self.expected_pid: int | None = None
        # Live owner-side completion only. A visible .done file after failed
        # fsync is not a durable receipt and must never be promoted by itself.
        self._server: asyncio.AbstractServer | None = None
        self._created = False
        self.calls: NativeToolCallTable[Call] = NativeToolCallTable()
        self._handlers: set[asyncio.Task] = set()

    @abstractmethod
    def decode_call(self, call_id: object, name: object, arguments: object) -> Call: ...

    @abstractmethod
    def decode_request(self, raw: bytes) -> Call: ...

    @abstractmethod
    async def admit(self, round_number: int, call: Call) -> None: ...

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

    @property
    def selected_call_id(self) -> str | None:
        return None

    async def handle_request(self, raw: bytes) -> dict[str, object]:
        call = self.calls.call_for(self.decode_request(raw))
        await call.authorize(lambda: self.admit(self.calls.identity_for(call).round_number, call))
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


class SelectedToolSocket(OwnerToolSocket[SelectedToolRequest]):
    """One selected replacement, preserving the existing one-slot authority."""

    def __init__(
        self, directory: Path, token: str, action: Callable[[SelectedToolRequest], None]
    ) -> None:
        super().__init__(directory, token)
        self.action = action

    def validate_round(self, round: NativeToolRound[SelectedToolRequest]) -> None:
        if len(round.declared) != 1 or self.calls.rounds:
            raise SelectedToolDenied("Native Pi returned more than one selected call")

    def decode_call(self, call_id: object, name: object, arguments: object) -> SelectedToolRequest:
        if name != "selected_claimed_write":
            raise SelectedToolDenied("Native Pi returned an unapproved selected call")
        try:
            return FieldCodec.decode(
                SelectedToolRequest, {"call_id": call_id, "arguments": arguments}
            )
        except (TypeError, ValueError) as error:
            raise SelectedToolDenied("Invalid selected tool call") from error

    def decode_request(self, raw: bytes) -> SelectedToolRequest:
        if len(raw) > self.max_request or not raw.endswith(b"\n"):
            raise SelectedToolDenied("Selected tool request is incomplete or oversized")
        try:
            envelope = FieldCodec.decode(
                SelectedToolEnvelope,
                json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields),
            )
        except (TypeError, ValueError) as error:
            raise SelectedToolDenied("Invalid selected tool request") from error
        return envelope.authenticate(self.token)

    async def admit(self, round_number: int, call: SelectedToolRequest) -> None:
        await Coordination.run_worker(partial(self.action, call))

    @property
    def selected_call_id(self) -> str | None:
        return self.calls.first_call_id
