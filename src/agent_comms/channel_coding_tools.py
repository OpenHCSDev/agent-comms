"""Normal Pi coding tools authorized by the selected channel turn's existing owner.

Pi owns actual read/bash/edit/write execution. This module owns the cooperative
pre-tool claim check, using the bus's existing claims and native input ledger.
Shell is not a filesystem sandbox; shell work must respect the injected claims.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_comms.coordinator import Coordination

from .selected_claim import (
    publish_selected_resource_claim,
    verify_selected_wake,
)
from .comms import Comms
from .envelope_claim_transitions import (
    ClaimOwner,
    WakeClaim,
)
from .native_tool_call import NativeToolCall, SelectedToolDenied
from .native_tools import NativeExecutableTool
from .storage_rules import StorageDecision
from .selected_tool_broker import (
    NativeToolMode,
    OwnerToolSocket,
    consume_selected_slot,
    record_selected_terminal,
    verify_sent_full_input,
)


@dataclass
class CodingCall(NativeToolCall):
    tool: NativeExecutableTool
    storage_decisions: tuple[StorageDecision, ...] = field(default=(), init=False, compare=False, repr=False)

    @property
    def name(self) -> str:
        return self.tool.declared_name

    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None:
        for decision in self.storage_decisions:
            decision.complete()
        slot = self.slot(input_id, round_number)
        record_selected_terminal(directory, slot, slot)

    def claim_failed(self) -> None:
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
            return NativeExecutableTool.from_call(name, arguments, call_id=call_id)
        except (ValueError, TypeError, KeyError) as error:
            raise SelectedToolDenied(f"Invalid native coding call: {error}") from error

    def slot(self, input_id: str, round_number: int) -> str:
        identity = f"{input_id}\0{round_number}\0{self.call_id}"
        return hashlib.sha256(identity.encode()).hexdigest()[:32]


@dataclass
class CoordinationCall(CodingCall):
    """The declaration executes through Core; raw arguments still own exact call matching."""

    tool: type[NativeExecutableTool]
    arguments: dict[str, object]


@dataclass
class CodingToolOwner:
    comms: Comms
    store: Coordination
    claim_scope: WakeClaim
    owner_name: str
    session_dir: Path
    input_id: str
    claims: dict[str, ClaimOwner] = field(default_factory=dict)
    check_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def for_original(self, assignment, operation_id) -> CodingToolOwner:
        """Project the same native grant onto an actually included original."""
        from .native_runtime_input import NativeRuntimeInput

        with self.store.session.read():
            native = NativeRuntimeInput.one(self.store.session._connection, input_id=self.input_id)
            if native is None or assignment.assignment_id not in native.execution.source_assignment_ids(
                self.store.session._connection, self.input_id
            ):
                raise SelectedToolDenied("Tool original is absent from the reserved native batch")
        return replace(self, claim_scope=replace(
            self.claim_scope,
            source_seq=assignment.wire_seq,
            source_message_id=assignment.message_id,
            wake_assignment_id=assignment.assignment_id,
            wake_revision=assignment.revision,
            operation_id=operation_id,
        ))

    async def allow(self, round_number: int, call: CodingCall) -> None:
        def check(store: Coordination) -> None:
            verify_sent_full_input(store, self.claim_scope, self.owner_name, self.input_id)
            verify_selected_wake(self.comms, store, self.claim_scope, self.owner_name)
            slot = call.slot(self.input_id, round_number)
            consume_selected_slot(self.session_dir, slot, slot)
            owner = self.comms.registry.require(self.owner_name)
            try:
                effects = call.tool.storage_effects(Path(owner.worktree))
                if effects:
                    call.storage_decisions = (self.comms.registry.decide_storage(self.owner_name, effects),)
            except SelectedToolDenied as error:
                raise SelectedToolDenied(f'{error}\n{self.comms.registry.storage_rule.render(owner.storage_roots)}') from error
            resource = call.tool.claim
            if resource is not None:
                canonical = resource.normalized(Path(owner.worktree))
                prior = self.claims.get(canonical)
                claim_authority = (
                    prior.claim_scope
                    if prior is not None
                    else replace(self.claim_scope, operation_id=slot)
                )
                claimed = publish_selected_resource_claim(
                    self.comms, store, claim_authority, self.owner_name, resource
                )
                self.claims[canonical] = claimed

        # Shared original-input views keep one claim map. Serialize its checks;
        # the native tools themselves remain free to execute in parallel.
        async with self.check_lock:
            await Coordination.run_async(self.store.session.path, check)


@dataclass(frozen=True)
class CodingToolMode(NativeToolMode):
    owner: CodingToolOwner

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        declarations = NativeExecutableTool.members_with(NativeExecutableTool)
        extensions = tuple(dict.fromkeys(tool.native_extension(package) for tool in declarations))
        # The native package manifest verifies each declaration's extension bytes.
        if any(not extension.is_file() for extension in extensions):
            raise SelectedToolDenied("Native tool extension is not installed")
        return ("--tools", ",".join(tool.declared_name for tool in declarations),
                *(argument for extension in extensions for argument in ("-e", str(extension))))

    async def open_transport(self, turn, ownership) -> OwnerToolSocket:
        return await CodingToolSocket.open(turn, ownership, self.owner)

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
        from .jsonl_stream import unique_fields

        value = json.loads(raw, object_pairs_hook=unique_fields)
        if (
            type(value) is not dict
            or set(value) != {"token", "call_id", "name", "arguments"}
            or value["token"] != self.token
        ):
            raise SelectedToolDenied("Coding request is not authenticated")
        return CodingCall.decode(value["call_id"], value["name"], value["arguments"])

    async def allow(self, round_number: int, call: CodingCall) -> None:
        await self.owner.allow(round_number, call)

    def failure_response(self, error: Exception) -> dict[str, object]:
        return {"ok": False, "error": str(error)}
