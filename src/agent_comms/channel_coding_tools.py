"""Normal Pi coding tools admitted by the selected channel turn's existing owner.

Pi owns actual read/bash/edit/write execution. This module owns the cooperative
pre-tool claim check, using the bus's existing claims and native input ledger.
Shell is not a filesystem sandbox; shell work must respect the injected claims.
"""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .claim_admission import (
    publish_selected_resource_claim,
    release_selected_resources,
    verify_selected_wake,
)
from .comms import Comms
from .coordination_store import MutationStore
from .declared_family import DeclaredFamily
from .envelope_claim_transitions import (
    ClaimOwner,
    ExistingFileClaim,
    FileClaimPath,
    WakeAdmission,
    WritableFileClaim,
)
from .field_codec import FieldCodec
from .native_tool_call import NativeToolCall, SelectedToolDenied
from .selected_tool_broker import (
    NativeToolMode,
    OwnerToolSocket,
    consume_selected_slot,
    record_selected_terminal,
    verify_sent_full_input,
)


@dataclass(frozen=True)
class CodingTool(DeclaredFamily, affix="Tool"):
    """Pi owns argument schemas; this family owns only cooperative claim behavior."""

    arguments: dict[str, Any]

    @abstractmethod
    def resource_claim(self) -> FileClaimPath | None: ...

    @classmethod
    def from_call(cls, name: str, arguments: dict[str, Any]) -> CodingTool:
        # The native tool validates its full schema. Preserve it exactly for
        # event/socket correlation, without maintaining a second Pi schema.
        FieldCodec.encode(arguments)
        tool = cls.decode(name)(arguments)
        resource = tool.resource_claim()
        if resource is not None and (type(resource.resource) is not str or not resource.resource):
            raise SelectedToolDenied("Coding mutation requires a file path")
        return tool


class ReadTool(CodingTool):
    def resource_claim(self) -> None:
        return None


class BashTool(CodingTool):
    def resource_claim(self) -> None:
        return None


class EditTool(CodingTool):
    def resource_claim(self) -> FileClaimPath:
        return ExistingFileClaim(self.arguments["path"])


class WriteTool(CodingTool):
    def resource_claim(self) -> FileClaimPath:
        return WritableFileClaim(self.arguments["path"])


@dataclass
class CodingCall(NativeToolCall):
    tool: CodingTool

    @property
    def name(self) -> str:
        return self.tool.declared_name

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
    store: MutationStore
    admission: WakeAdmission
    owner_name: str
    session_dir: Path
    input_id: str
    claims: dict[str, ClaimOwner] = field(default_factory=dict)

    def admit(self, call: CodingCall) -> None:
        verify_sent_full_input(self.store, self.admission, self.owner_name, self.input_id)
        verify_selected_wake(self.comms, self.store, self.admission, self.owner_name)
        consume_selected_slot(self.session_dir, call.slot(self.input_id), call.slot(self.input_id))
        resource = call.tool.resource_claim()
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

    def finish(self) -> None:
        release_selected_resources(
            self.comms, self.store, self.admission, self.owner_name, tuple(self.claims.values())
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

    def finish(self) -> None:
        self.owner.finish()


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
