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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .claim_admission import publish_selected_resource_claim, write_selected_claimed_file
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_store import MutationStore
from .envelope_claim_transitions import WakeAdmission
from .operations import Comms

# Stay well below the existing native RPC record cap (1 MiB, including JSON).
_MAX_CONTENT = 128 * 1024
_MAX_REQUEST = _MAX_CONTENT + 8192
_INPUT_ID = re.compile(r"[0-9a-f]{32}\Z")
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_TOOL_SOURCE_SHA = "b6577147072d959632ed2318bd11645b019d6bccc76ab6d24fc112ac2da43e58"


@dataclass(frozen=True, slots=True)
class SelectedToolIntent:
    """Trusted, explicit pre-turn opt-in; no tool call or write authority.

    The owner must create a distinct bound SelectedToolMode only after reserving
    the exact FULL input. Do not construct this from injected/model text.
    """


@dataclass(frozen=True, slots=True)
class SelectedToolMode:
    """Trusted owner callback, never parsed from a model call or injected text."""

    action: Callable[[SelectedToolRequest], None]

    def __post_init__(self) -> None:
        if not callable(self.action):
            raise TypeError("Selected tool mode requires an owner callback")


class SelectedToolDenied(ValueError):  # noqa: N818 - nominal fail-closed outcome
    """A rejected request (including duplicate or uncertain prior consumption)."""


@dataclass(frozen=True, slots=True)
class SelectedToolRequest:
    call_id: str
    resource: str
    contents: bytes


def parse_selected_request(raw: bytes, token: str) -> SelectedToolRequest:
    """Strict bounded wire syntax; token authenticates transport, not admission."""
    if type(raw) is not bytes or len(raw) > _MAX_REQUEST or not raw.endswith(b"\n"):
        raise SelectedToolDenied("Selected tool request is incomplete or oversized")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SelectedToolDenied("Ambiguous selected tool request")
            result[key] = value
        return result

    try:
        value = json.loads(raw[:-1].decode("utf-8", errors="strict"), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as error:
        raise SelectedToolDenied("Invalid selected tool request") from error
    if type(value) is not dict or set(value) != {"token", "call_id", "resource", "contents"}:
        raise SelectedToolDenied("Selected tool fields are not exact")
    resource, contents, call_id = value["resource"], value["contents"], value["call_id"]
    if value["token"] != token or type(call_id) is not str or not _CALL_ID.fullmatch(call_id):
        raise SelectedToolDenied("Selected tool transport is not authenticated")
    if type(resource) is not str or not resource or len(resource.encode("utf-8")) > 4096:
        raise SelectedToolDenied("Selected tool resource is not bounded")
    path = Path(resource)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or resource.startswith("@")
        or resource.startswith("./")
    ):
        raise SelectedToolDenied("Selected tool resource must be relative without aliases")
    if type(contents) is not str:
        raise SelectedToolDenied("Selected tool contents must be UTF-8 text")
    try:
        payload = contents.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise SelectedToolDenied("Selected tool contents are not UTF-8") from error
    if len(payload) > _MAX_CONTENT:
        raise SelectedToolDenied("Selected tool contents exceed 128 KiB")
    return SelectedToolRequest(call_id, resource, payload)


def stage_selected_extension(directory: Path) -> Path:
    """Pin and durably stage only the reviewed explicit tool extension.

    Never load extension source directly from a project/worktree directory.
    Existing staged bytes are verified, never overwritten after an uncertain
    previous attempt. Source changes require a new reviewed hash.
    """
    source = Path(__file__).with_name("selected_claimed_write.mjs")
    info = source.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or hashlib.sha256(source.read_bytes()).hexdigest() != _TOOL_SOURCE_SHA
    ):
        raise SelectedToolDenied("Selected native extension differs from reviewed source")
    directory = Path(directory).absolute()
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or directory.resolve() != directory
    ):
        raise SelectedToolDenied("Selected tool staging directory is not private")
    target = directory / "selected-claim-extension.mjs"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(source.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            _sync_dir(directory)
        except OSError as error:
            raise SelectedToolDenied("Selected extension staging UNKNOWN") from error
    info = target.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or hashlib.sha256(target.read_bytes()).hexdigest() != _TOOL_SOURCE_SHA
    ):
        raise SelectedToolDenied("Selected extension is not pinned and private")
    return target


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
    if type(input_id) is not str or not _INPUT_ID.fullmatch(input_id):
        raise SelectedToolDenied("Selected input identity is invalid")
    if type(call_id) is not str or not _CALL_ID.fullmatch(call_id):
        raise SelectedToolDenied("Selected tool call identity is invalid")
    directory = Path(directory).absolute()
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or directory.resolve() != directory
    ):
        raise SelectedToolDenied("Selected tool session directory is not private")
    ledger = directory / "selected-tool-ledger"
    try:
        ledger.mkdir(mode=0o700, exist_ok=True)
        info = ledger.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
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
    store: MutationStore,
    admission: WakeAdmission,
    owner_name: str,
    session_dir: Path,
    input_id: str,
) -> SelectedToolMode:
    """Capture only owner-derived typed fields for this one FULL selected input.

    Construct after the owner has engaged the selected attempt and reserved its
    tracked input. Never derive this mode from user/model/wake-injected text.
    """
    if (
        type(comms) is not Comms
        or type(store) is not MutationStore
        or type(admission) is not WakeAdmission
        or type(owner_name) is not str
        or type(input_id) is not str
        or not _INPUT_ID.fullmatch(input_id)
    ):
        raise SelectedToolDenied("Selected mode has no typed owner attempt")

    def owner_action(request: SelectedToolRequest) -> None:
        # The prompt-send boundary commits this epoch to the exact reserved
        # FULL input before Pi can receive it. A socket token, PID, model text,
        # or context journal alone does not establish this source binding.
        with MutationStore(str(store.path), lock_timeout=0) as scoped, scoped._read_transaction():
            assert_native_runtime_schema(scoped._connection)
            row = scoped._connection.execute(
                "SELECT * FROM native_runtime_inputs WHERE input_id=?", (input_id,)
            ).fetchone()
            if row is None or (
                row["stage"],
                row["claim_id"],
                row["execution_id"],
                row["attempt_ordinal"],
                row["owner_lookup"],
                row["owner_thread"],
                row["owner_generation"],
                row["sent_owner_admission_epoch"],
                row["session_id"],
                row["verdict"],
            ) != (
                "full",
                admission.wake_claim_id,
                admission.execution_id,
                admission.attempt_ordinal,
                admission.recipient_lookup,
                owner_name,
                admission.participant_generation,
                admission.owner_admission_generation,
                None,
                None,
            ):
                raise SelectedToolDenied("Selected tool does not match the exact sent FULL input")
        perform_selected_write(comms, store, admission, owner_name, session_dir, input_id, request)

    return SelectedToolMode(owner_action)


def verify_selected_terminal(directory: Path, input_id: str, call_id: str) -> None:
    """Corroborate a live owner result; a persisted receipt alone grants nothing."""
    if type(input_id) is not str or not _INPUT_ID.fullmatch(input_id):
        raise SelectedToolDenied("Selected input identity is invalid")
    receipt = Path(directory).absolute() / "selected-tool-ledger" / (input_id + ".done")
    info = receipt.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or receipt.read_bytes() != (call_id + "\n").encode("ascii")
    ):
        raise SelectedToolDenied("Selected tool has no matching terminal receipt")


def perform_selected_write(
    comms: Comms,
    store: MutationStore,
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
    claimed = publish_selected_resource_claim(comms, store, admission, owner_name, request.resource)
    write_selected_claimed_file(comms, store, admission, owner_name, claimed, request.contents)
    record_selected_terminal(session_dir, input_id, request.call_id)


class SelectedToolSocket:
    """One-shot authenticated child-to-owner IPC; no bearer token is admission."""

    def __init__(
        self,
        directory: Path,
        token: str,
        action: Callable[[SelectedToolRequest], None],
    ) -> None:
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise ValueError("Selected tool transport requires a random 256-bit token")
        self.path = Path(directory).absolute() / "selected-tool.sock"
        self.token = token
        self.action = action
        self.expected_pid: int | None = None
        # Live owner-side completion only. A visible .done file after failed
        # fsync is not a durable receipt and must never be promoted by itself.
        self.completed_call_id: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._approved: dict[str, tuple[str, bytes]] = {}
        self._approval_changed = asyncio.Event()
        self._created = False

    def approve_tool_start(self, call_id: str, arguments: object) -> None:
        """Owner supplies a Pi-emitted tool_execution_start, never model text."""
        if (
            type(call_id) is not str
            or not _CALL_ID.fullmatch(call_id)
            or type(arguments) is not dict
            or set(arguments) != {"resource", "contents"}
        ):
            return
        if type(arguments["resource"]) is not str or type(arguments["contents"]) is not str:
            return
        try:
            contents = arguments["contents"].encode("utf-8", errors="strict")
        except UnicodeError:
            return
        if len(contents) > _MAX_CONTENT:
            return
        self._approved[call_id] = (arguments["resource"], contents)
        self._approval_changed.set()

    async def start(self) -> None:
        if not hasattr(socket, "SO_PEERCRED") or not hasattr(os, "O_NOFOLLOW"):
            raise SelectedToolDenied("Selected tool requires a peer-credential Unix socket")
        info = self.path.parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or self.path.parent.resolve() != self.path.parent
        ):
            raise SelectedToolDenied("Selected tool socket parent is not private")
        if self.path.exists() or self.path.is_symlink():
            raise SelectedToolDenied("Selected tool socket already exists")
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.path), limit=_MAX_REQUEST + 1
        )
        self._created = True
        try:
            os.chmod(self.path, 0o600)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._created:
            self.path.unlink(missing_ok=True)
            self._created = False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            if sock is None or self.expected_pid is None:
                raise SelectedToolDenied("Selected tool child is not bound")
            pid, uid, _gid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if pid != self.expected_pid or uid != os.geteuid():
                raise SelectedToolDenied("Selected tool peer is not the launched Pi child")
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            request = parse_selected_request(raw, self.token)
            if request.call_id not in self._approved:
                await asyncio.wait_for(self._approval_changed.wait(), timeout=10)
            if self._approved.get(request.call_id) != (request.resource, request.contents):
                raise SelectedToolDenied("Selected tool call differs from owner's Pi event")
            # Sync action owns the durable consumption and writer call. A
            # second socket may queue but cannot pass the one-slot ledger.
            self.action(request)
            self.completed_call_id = request.call_id
            response = {"ok": True}
        except Exception:
            # Do not leak details about root, path, admission or file content.
            response = {"ok": False, "error": "Selected tool denied or outcome UNKNOWN; no retry"}
        try:
            writer.write((json.dumps(response) + "\n").encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
