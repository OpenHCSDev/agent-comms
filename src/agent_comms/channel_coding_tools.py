"""Normal Pi coding tools admitted by the selected channel turn's existing owner.

Pi owns actual read/bash/edit/write execution. This module owns the cooperative
pre-tool claim check, using the bus's existing claims and native input ledger.
Shell is not a filesystem sandbox; shell work must respect the injected claims.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_comms.coordinator import Coordination

from .claim_admission import (
    publish_selected_resource_claim,
    release_selected_resources,
    verify_selected_wake,
)
from .comms import Comms
from .field_codec import FieldCodec
from .envelope_claim_transitions import (
    ClaimOwner,
    WakeAdmission,
)
from .native_tool_call import NativeToolCall, SelectedToolDenied
from .native_tools import CodingTool
from .selected_tool_broker import (
    NativeToolMode,
    OwnerToolSocket,
    consume_selected_slot,
    record_selected_terminal,
    verify_sent_full_input,
)


@dataclass
class CodingCall(NativeToolCall):
    tool: CodingTool

    @property
    def name(self) -> str:
        return FieldCodec.encode(type(self.tool))

    def commit_terminal(self, is_error: bool, directory: Path, input_id: str) -> None:
        record_selected_terminal(directory, self.slot(input_id), self.slot(input_id))

    def admission_failed(self) -> None:
        # A denied ordinary tool may report an error and let Pi explain it. The
        # consumed call cannot be retried; this grants no mutation authority.
        pass

    @classmethod
    def decode(cls, call_id: object, name: object, arguments: object) -> CodingCall:
        if (
            type(call_id) is not str
            or not call_id
            or len(call_id) > 256
            or type(name) is not str
            or type(arguments) is not dict
        ):
            raise SelectedToolDenied("Malformed native coding call")
        try:
            return cls(call_id, CodingTool.from_call(name, arguments))
        except (ValueError, TypeError, KeyError) as error:
            raise SelectedToolDenied(f"Invalid native coding call: {error}") from error

    def slot(self, input_id: str) -> str:
        return hashlib.sha256((input_id + "\0" + self.call_id).encode()).hexdigest()[:32]


@dataclass
class CodingToolOwner:
    comms: Comms
    store: Coordination
    admission: WakeAdmission
    owner_name: str
    session_dir: Path
    input_id: str
    claims: dict[str, ClaimOwner] = field(default_factory=dict)

    def for_original(self, assignment, operation_id) -> CodingToolOwner:
        """Project the same native grant onto an actually included original."""
        from .native_runtime_input import NativeRuntimeInput

        with self.store.session.read():
            native = NativeRuntimeInput.one(self.store.session._connection, input_id=self.input_id)
            if native is None or assignment.assignment_id not in native.execution.source_assignment_ids(
                self.store.session._connection, self.input_id
            ):
                raise SelectedToolDenied("Tool original is absent from the reserved native batch")
        return replace(self, admission=replace(
            self.admission,
            source_seq=assignment.wire_seq,
            source_message_id=assignment.message_id,
            wake_assignment_id=assignment.assignment_id,
            wake_revision=assignment.revision,
            operation_id=operation_id,
        ))

    def admit(self, call: CodingCall) -> None:
        verify_sent_full_input(self.store, self.admission, self.owner_name, self.input_id)
        verify_selected_wake(self.comms, self.store, self.admission, self.owner_name)
        consume_selected_slot(self.session_dir, call.slot(self.input_id), call.slot(self.input_id))
        resource = call.tool.claim
        if resource is not None:
            owner = self.comms.registry.require(self.owner_name)
            canonical = resource.normalized(Path(owner.worktree))
            prior = self.claims.get(canonical)
            claim_admission = (
                prior.admission
                if prior is not None
                else replace(self.admission, operation_id=call.slot(self.input_id))
            )
            claimed = publish_selected_resource_claim(
                self.comms, self.store, claim_admission, self.owner_name, resource
            )
            self.claims[canonical] = claimed

    async def finish(self) -> None:
        """Only an owned claim set needs a worker and its SQLite resource."""
        if not self.claims:
            return
        await Coordination.run_async(
            self.store.session.path, self.release,
            clock_ms=self.store.session.now,
        )

    def release(self, store: Coordination) -> None:
        release_selected_resources(
            self.comms, store, self.admission, self.owner_name, tuple(self.claims.values())
        )
        self.claims.clear()


@dataclass(frozen=True)
class CodingToolMode(NativeToolMode):
    owner: CodingToolOwner

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        extension = Path(package) / "dist/channel_coding_tools.mjs"
        # The copied native package manifest verifies the extension bytes.
        if not extension.is_file():
            raise SelectedToolDenied("Native coding extension is not installed")
        return ("--tools", ",".join(CodingTool.names()), "-e", str(extension))

    def socket(self, directory: Path, token: str) -> OwnerToolSocket:
        return CodingToolSocket(directory, token, self.owner)

    async def finish(self) -> None:
        await self.owner.finish()


class CodingToolSocket(OwnerToolSocket[CodingCall]):
    """Multi-call native policy on the same peer-authenticated owner transport."""

    max_request = 1024 * 1024

    def __init__(self, directory: Path, token: str, owner: CodingToolOwner) -> None:
        super().__init__(directory, token)
        self.owner = owner

    def decode_call(self, call_id: object, name: object, arguments: object) -> CodingCall:
        return CodingCall.decode(call_id, name, arguments)

    def decode_request(self, raw: bytes) -> CodingCall:
        if not raw.endswith(b"\n") or len(raw) > self.max_request:
            raise SelectedToolDenied("Coding request incomplete or oversized")
        from .native_pi import _unique

        value = json.loads(raw, object_pairs_hook=_unique)
        if (
            type(value) is not dict
            or set(value) != {"token", "call_id", "name", "arguments"}
            or value["token"] != self.token
        ):
            raise SelectedToolDenied("Coding request is not authenticated")
        return CodingCall.decode(value["call_id"], value["name"], value["arguments"])

    def admit(self, call: CodingCall) -> None:
        self.owner.admit(call)

    def failure_response(self, error: Exception) -> dict[str, object]:
        return {"ok": False, "error": str(error)}
