"""Default-off, one-slot owner boundary for a selected Pi tool request.

The model supplies only a relative existing-file resource and UTF-8 replacement
text. The owner supplies the identities and permission checks; neither a prompt nor
a socket message can authorize a wake. Consumption is durable before any
claim append or file mutation, and UNKNOWN is never retried automatically.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.child_process import Platform
from agent_comms.coordinator import Coordination

from .comms import Comms
from .child_process import Platform
from agent_comms.native_runtime_input import NativeRuntimeTable
from .coordination_errors import CoordinationReadUnavailable
from .coordination_tables.participants import OwnerGenerations
from .envelope_claim_transitions import ExistingFileClaim, WakeClaim
from .field_codec import FieldCodec, Id128Text, MalformedRecord
from .native_input_record import FullNativeExecution, NativeInputIdentity
from .native_runtime_input import NativeRuntimeInput
from .native_tool_call import (
    CoordinationBusy,
    NativeToolCall,
    NativeToolMode,
    SelectedToolDenied,
)
from .native_tool_transport import (
    NativeToolRound,
    ToolListener,
    ToolTransport,
    UnboundToolTransport,
)
from .private_path import PrivateDirectoryRole, PrivateFileRole
from .selected_actions import SelectedAction
from .selected_claim import publish_selected_resource_claim, write_selected_claimed_file
from .turn_context import InstructionFile

# Stay well below the existing native RPC record cap (1 MiB, including JSON).
_MAX_CONTENT = 128 * 1024
_MAX_REQUEST = _MAX_CONTENT + 8192
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOOL_SOURCE = Path(__file__).with_name("selected_claimed_write.mjs")

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SelectedToolIntent(SelectedAction):
    """Trusted, explicit pre-turn opt-in; no tool call or write authority.

    The owner must create a distinct bound SelectedToolMode only after reserving
    the exact FULL input. Do not construct this from injected/model text.
    """

    @property
    def instruction_files(self) -> tuple[InstructionFile, ...]:
        return (InstructionFile.read("selected-claimed-write.md"),)

    def mode(self, owner):
        return selected_tool_mode_for_owner(
            owner.comms,
            owner.store,
            owner.claim_scope,
            owner.owner_name,
            owner.session_dir,
            owner.input_id,
        )


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

    async def open_transport(self, turn, ownership) -> UnboundToolTransport:
        return await SelectedToolSocket.open(turn, ownership, self.action)


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


@dataclass(frozen=True)
class SelectedToolRequest(NativeToolCall):
    arguments: SelectedWriteArguments

    def __post_init__(self) -> None:
        if not _CALL_ID.fullmatch(self.call_id):
            raise SelectedToolDenied("Selected tool call identity is invalid")

    declared_name: ClassVar[str] = "selected_claimed_write"

    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None:
        if is_error:
            raise SelectedToolDenied("Native Pi selected tool did not finish successfully")
        verify_selected_terminal(directory, input_id, self.call_id)

    def claim_failed(self) -> None:
        raise SelectedToolDenied("Native Pi selected tool claim_scope failed; outcome UNKNOWN")


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

    The native deployment manifest allows this exact module. A private session
    directory is state storage, not an extension installation root.
    """
    source = Path(package) / "dist/selected_claimed_write.mjs"
    info = source.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or source.read_bytes() != _TOOL_SOURCE.read_bytes()
    ):
        raise SelectedToolDenied("Selected native extension differs from reviewed source")
    return source


def consume_selected_slot(directory: Path, input_id: str, call_id: str) -> None:
    """Persist one consumed slot BEFORE a bus append; even failed fsync is UNKNOWN.

    The caller must provide a private, physical owner-owned session directory.
    An existing slot is never reopened or reclaimed, even after a process crash.
    """
    try:
        Id128Text.decode(input_id)
    except MalformedRecord as error:
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
        Platform.current().sync_directory(directory)
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
        Platform.current().sync_directory(ledger)
    except OSError as error:
        # Do NOT remove the visible slot; a failed fsync or a crash may have
        # committed it. No automatic re-append, retry, or write follows.
        raise SelectedToolDenied("Selected tool consumption UNKNOWN; do not retry") from error


def record_selected_terminal(directory: Path, input_id: str, call_id: str) -> None:
    """Only a synced terminal acknowledgement lets the tool return success."""
    result = Path(directory).absolute() / "selected-tool-ledger" / (input_id + ".done")
    try:
        descriptor = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write((call_id + "\n").encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        Platform.current().sync_directory(result.parent)
    except OSError as error:
        raise SelectedToolDenied("Selected tool terminal acknowledgement UNKNOWN; no retry") from error


def selected_tool_mode_for_owner(
    comms: Comms,
    store: Coordination,
    claim_scope: WakeClaim,
    owner_name: str,
    session_dir: Path,
    input_id: str,
) -> SelectedToolMode:
    """Capture only owner-derived typed fields for this one FULL selected input.

    Construct after the owner has engaged the selected attempt and reserved its
    tracked input. Never derive this mode from user/model/wake-injected text.
    """
    try:
        Id128Text.decode(input_id)
        FieldCodec.decode(str, owner_name)
    except MalformedRecord as error:
        raise SelectedToolDenied("Selected mode has no typed owner attempt") from error

    path = store.session.path

    def owner_action(request: SelectedToolRequest) -> None:
        # The prompt-send boundary commits this epoch to the exact reserved
        # FULL input before Pi can receive it. A socket token, PID, model text,
        # or context journal alone does not establish this source binding.
        with Coordination(path) as worker_store:
            verify_sent_full_input(worker_store, claim_scope, owner_name, input_id)
            perform_selected_write(
                comms, worker_store, claim_scope, owner_name, session_dir, input_id, request
            )

    return SelectedToolMode(owner_action)


def verify_sent_full_input(
    store: Coordination, claim_scope: WakeClaim, owner_name: str, input_id: str
) -> None:
    """Only the exact FULL input authorized by the owner may call native tools.

    This is the first check of a tool call and it only reads. Other owners
    write the same database, so the read waits the coordinator's bounded
    time; a database still busy after that is reported as busy, not as a
    refusal of the call and not as an unknown outcome.
    """
    try:
        with CoordinationReadUnavailable.checking(), Coordination(str(store.session.path)) as scoped, scoped.session.read():
            _require_sent_full_input(scoped, claim_scope, owner_name, input_id)
    except CoordinationReadUnavailable as unavailable:
        raise CoordinationBusy() from unavailable


def _require_sent_full_input(
    scoped: Coordination, claim_scope: WakeClaim, owner_name: str, input_id: str
) -> None:
    NativeRuntimeTable.require_installed(scoped.session._connection)
    row = NativeRuntimeInput.one(scoped.session._connection, input_id=input_id)
    if row is None:
        raise SelectedToolDenied("Selected tool has no reserved FULL input")
    if claim_scope.wake_assignment_id not in row.execution.source_assignment_ids(
        scoped.session._connection, input_id
    ):
        raise SelectedToolDenied("Selected tool source was not included in this native input")
    expected = NativeInputIdentity(
        input_id, FullNativeExecution(claim_scope.execution_id, claim_scope.attempt_ordinal),
        OwnerGenerations(
            owner_lookup=claim_scope.recipient_lookup,
            owner_thread=owner_name,
            generation=claim_scope.participant_generation,
        ),
    )
    if row.identity != expected:
        raise SelectedToolDenied("Selected tool does not match the exact sent FULL input")
    if not row.sent_owner_claimed_generation.matches(claim_scope.owner_claimed_generation):
        raise SelectedToolDenied("Selected tool names another sending claim_scope")
    if row.reference.recorded or row.verdict.decided:
        raise SelectedToolDenied("Selected input was already settled")


def verify_selected_terminal(directory: Path, input_id: str, call_id: str) -> None:
    """Corroborate a live owner result; a persisted acknowledgement alone grants nothing."""
    try:
        Id128Text.decode(input_id)
    except MalformedRecord as error:
        raise SelectedToolDenied("Selected input identity is invalid") from error
    acknowledgement = Path(directory).absolute() / "selected-tool-ledger" / (input_id + ".done")
    info = acknowledgement.lstat()
    if PrivateFileRole.violation(info) is not None:
        raise SelectedToolDenied("Selected tool acknowledgement is not private")
    if acknowledgement.read_bytes() != (call_id + "\n").encode("ascii"):
        raise SelectedToolDenied("Selected tool has no matching terminal acknowledgement")


def perform_selected_write(
    comms: Comms,
    store: Coordination,
    claim_scope: WakeClaim,
    owner_name: str,
    session_dir: Path,
    input_id: str,
    request: SelectedToolRequest,
) -> None:
    """Only the owner calls the existing selected claim and writer boundaries."""
    consume_selected_slot(session_dir, input_id, request.call_id)
    # Any exception from here leaves the slot consumed. The caller reports
    # UNKNOWN and never automatically reissues the claim or mutation.
    claimed = publish_selected_resource_claim(
        comms, store, claim_scope, owner_name, request.arguments.claim
    )
    write_selected_claimed_file(
        comms, store, claim_scope, owner_name, claimed, request.arguments.contents.encode("utf-8")
    )
    record_selected_terminal(session_dir, input_id, request.call_id)


class SelectedToolSocket(ToolTransport[SelectedToolRequest]):
    max_request = _MAX_REQUEST
    """One selected replacement, preserving the existing one-slot authority."""

    def __init__(
        self, listener: ToolListener, action: Callable[[SelectedToolRequest], None], *, pid: int
    ) -> None:
        super().__init__(listener, pid=pid)
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
        except MalformedRecord as error:
            raise SelectedToolDenied("Invalid selected tool call") from error

    def decode_request(self, raw: bytes) -> SelectedToolRequest:
        if len(raw) > self.max_request or not raw.endswith(b"\n"):
            raise SelectedToolDenied("Selected tool request is incomplete or oversized")
        try:
            envelope = FieldCodec.decode(
                SelectedToolEnvelope,
                FieldCodec.parse(raw),
            )
        except MalformedRecord as error:
            raise SelectedToolDenied("Invalid selected tool request") from error
        return envelope.authenticate(self.listener.token)

    async def allow(self, round_number: int, call: SelectedToolRequest) -> None:
        await Coordination.run_worker(partial(self.action, call))
