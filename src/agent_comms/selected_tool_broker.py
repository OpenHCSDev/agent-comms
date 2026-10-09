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
import os
import re
import socket
import stat
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

from agent_comms.coordinator import Coordination

from .claim_admission import publish_selected_resource_claim, write_selected_claimed_file
from .comms import Comms
from .coordinated_runtime_schema import assert_native_runtime_schema
from .envelope_claim_transitions import ExistingFileClaim, WakeAdmission
from .field_codec import FieldCodec
from .native_runtime_input import NativeRuntimeInput
from .native_input_record import NativeInputIdText, NativeInputIdentity, FullNativeExecution
from .coordination_tables.participants import OwnerGenerations
from .native_tool_call import NativeToolCall, SelectedToolDenied
from .pi_events import ToolExecutionEnd, ToolExecutionStart
from .pi_payloads import PiContent, ToolCallContent
from .private_path import PrivateDirectoryRole, PrivateFileRole, PrivateSocketRole
from .pi_rpc import unique_fields
from .selected_actions import SelectedAction

# Stay well below the existing native RPC record cap (1 MiB, including JSON).
_MAX_CONTENT = 128 * 1024
_MAX_REQUEST = _MAX_CONTENT + 8192
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_TOOL_SOURCE_SHA = "361bce4704c6830b4212894b005d9a191262e37c49e15b396afb2936ffd1a845"


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

    async def finish(self) -> None:
        """Release completed coding claims; selected proof keeps its old semantics."""
        return None

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

    def commit_terminal(self, is_error: bool, directory: Path, input_id: str) -> None:
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

    def owner_action(request: SelectedToolRequest) -> None:
        # The prompt-send boundary commits this epoch to the exact reserved
        # FULL input before Pi can receive it. A socket token, PID, model text,
        # or context journal alone does not establish this source binding.
        verify_sent_full_input(store, admission, owner_name, input_id)
        perform_selected_write(comms, store, admission, owner_name, session_dir, input_id, request)

    return SelectedToolMode(owner_action)


def verify_sent_full_input(
    store: Coordination, admission: WakeAdmission, owner_name: str, input_id: str
) -> None:
    """Only the exact FULL input admitted by the owner may call native tools."""
    with Coordination(str(store.session.path), lock_timeout=0) as scoped, scoped.session.read():
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
        self.calls: dict[str, Call] = {}
        self._handlers: set[asyncio.Task] = set()

    @abstractmethod
    def decode_call(self, call_id: object, name: object, arguments: object) -> Call: ...

    @abstractmethod
    def decode_request(self, raw: bytes) -> Call: ...

    @abstractmethod
    def admit(self, call: Call) -> None: ...

    def call_for(self, candidate: Call) -> Call:
        existing = self.calls.get(candidate.call_id)
        if existing is not None:
            existing.correlate(candidate)
            return existing
        self.calls[candidate.call_id] = candidate
        return candidate

    def announce(self, content: tuple[PiContent, ...]) -> None:
        if not any(isinstance(item, ToolCallContent) for item in content):
            raise SelectedToolDenied("Native tool round has no declared call")
        for item in content:
            if isinstance(item, ToolCallContent):
                self.call_for(self.decode_call(item.id, item.name, item.arguments)).announce()
            elif not item.tool_round_allowed:
                raise SelectedToolDenied("Invalid native tool content")

    def tool_started(self, event: ToolExecutionStart) -> None:
        candidate = self.decode_call(event.tool_call_id, event.tool_name, event.args)
        call = self.calls.get(candidate.call_id)
        if call is None:
            raise SelectedToolDenied("Native tool start has no declared call")
        call.correlate(candidate)
        call.start()

    def tool_finished(self, event: ToolExecutionEnd, input_id: str) -> None:
        call = self.calls.get(event.tool_call_id)
        if call is None:
            raise SelectedToolDenied("Native tool terminal has no declared call")
        call.finish(event.tool_name, event.is_error, self.path.parent, input_id)

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
        call = self.call_for(self.decode_request(raw))
        await call.authorize(lambda: self.admit(call))
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
            except (ValueError, TimeoutError) as error:
                # SelectedToolDenied (a ValueError) is every typed denial and
                # UNKNOWN outcome; ValueError also covers the untrusted child's
                # undecodable request. Any other failure is a defect: it
                # propagates and the child sees a closed socket, never a reply.
                response = self.failure_response(error)
            writer.write((json.dumps(response) + "\n").encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            try:
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

    def call_for(self, candidate: SelectedToolRequest) -> SelectedToolRequest:
        if self.calls and candidate.call_id not in self.calls:
            raise SelectedToolDenied("Native Pi returned more than one selected call")
        return super().call_for(candidate)

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

    def admit(self, call: SelectedToolRequest) -> None:
        self.action(call)

    @property
    def selected_call_id(self) -> str | None:
        return next(iter(self.calls), None)
