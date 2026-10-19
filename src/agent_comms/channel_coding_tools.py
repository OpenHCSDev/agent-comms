"""Normal Pi coding tools authorized by the selected channel turn's existing owner.

Pi owns actual read/bash/edit/write execution. This module owns the cooperative
pre-tool claim check, using the bus's existing claims and native input ledger.
Shell is not a filesystem sandbox; shell work must respect the injected claims.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_comms.coordinator import Coordination

from .selected_claim import (
    publish_selected_resource_claim,
    verify_selected_wake,
)
from .comms import Comms
from .field_codec import FieldCodec
from .envelope_claim_transitions import (
    ClaimOwner,
    FileClaimPath,
    WakeClaim,
)
from .native_tool_call import CodingCall, NativeToolMode, SelectedToolDenied
from .native_arguments import NativeArguments
from .native_tools import NativeToolDeclaration
from .native_tool_transport import ToolTransport, ToolCallIdentity
from .child_process import ProcessIdentity
from .selected_tool_broker import (
    consume_selected_slot,
    verify_sent_full_input,
)


@dataclass(frozen=True, kw_only=True)
class CodingToolOwner(ABC):
    comms: Comms
    owner_name: str
    claims: dict[str, ClaimOwner] = field(default_factory=dict)
    check_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    async def allow(self, turn, round_number: int, call: CodingCall) -> None:
        def check(store: Coordination) -> None:
            self.verify(store, turn, self.comms.registry.snapshot())
            slot = call.slot(turn.command.input_id, round_number)
            consume_selected_slot(turn.launch.session.directory, slot, slot)
            owner = self.comms.registry.require(self.owner_name)
            resource = call.allowed_claim()
            if resource is not None:
                canonical = resource.normalized(Path(owner.worktree))
                self.claims[canonical] = self.claim_resource(
                    store, turn, resource, slot, self.claims.get(canonical))

        async with self.check_lock:
            await Coordination.run_async(self.comms.root / 'coordination.sqlite3', check)

    @abstractmethod
    def verify(self, store, turn, snapshot): ...

    @abstractmethod
    def claim_resource(self, store, turn, resource, slot, prior): ...

    def for_original(self, assignment, operation_id):
        raise SelectedToolDenied("An ordinary input has no selected original")


@dataclass(frozen=True)
class SelectedCodingToolOwner(CodingToolOwner):
    store: Coordination
    claim_scope: WakeClaim
    session_dir: Path
    input_id: str

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

    def verify(self, store, turn, snapshot):
        if self.input_id != turn.command.input_id:
            raise SelectedToolDenied("Native tool input changed")
        verify_sent_full_input(store, self.claim_scope, self.owner_name, self.input_id)
        verify_selected_wake(self.comms, store, self.claim_scope, self.owner_name)

    def claim_resource(self, store, turn, resource, slot, prior):
        scope = prior.claim_scope if prior is not None else replace(self.claim_scope, operation_id=slot)
        return publish_selected_resource_claim(self.comms, store, scope, self.owner_name, resource)



@dataclass(frozen=True)
class CodingToolMode(NativeToolMode):
    owner: CodingToolOwner

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        declarations = NativeToolDeclaration.members_with(NativeToolDeclaration)
        arguments = ("--tools", ",".join(tool.declared_name for tool in declarations),
                     *NativeToolDeclaration.extension_arguments(package))
        thread = self.owner.comms.registry.require(self.owner.owner_name)
        return thread.native_launch_arguments(NativeArguments.parse(arguments), package)

    async def open_transport(self, turn, ownership) -> ToolTransport:
        return await CodingToolSocket.open(turn, ownership, self.owner)

class CodingToolSocket(ToolTransport[CodingCall]):
    """Multi-call native policy on the same peer-authenticated owner transport."""

    def __init__(self, directory: Path, token: str, owner: CodingToolOwner) -> None:
        super().__init__(directory, token)
        self.owner = owner
        self.reservations = {}
        self.completions = set()

    def decode_call(self, call_id: object, name: object, arguments: object) -> CodingCall:
        return CodingCall.decode(call_id, name, arguments)

    def decode_request(self, raw: bytes) -> CodingCall:
        if not raw.endswith(b"\n") or len(raw) > self.max_request:
            raise SelectedToolDenied("Coding request incomplete or oversized")
        value = FieldCodec.parse(raw)
        if (
            type(value) is not dict
            or set(value) != {"token", "call_id", "name", "arguments"}
            or value["token"] != self.token
        ):
            raise SelectedToolDenied("Coding request is not authenticated")
        return CodingCall.decode(value["call_id"], value["name"], value["arguments"])

    async def allow(self, round_number: int, call: CodingCall) -> None:
        registry = self.owner.comms.registry
        thread = registry.require(self.owner.owner_name)
        effects = call.storage_effects(Path(thread.worktree))
        await asyncio.to_thread(registry.decide_storage, thread.name, effects)
        work = tuple(dict.fromkeys(kind for effect in effects for kind in effect.work))
        reservation = None
        if work:
            reservation = await registry.storage_rule.wait_for_room(
                work, thread, self.owner.comms.root,
                process=ProcessIdentity.capture(self.expected_pid))
        try:
            # Recheck turn authority only after waiting, with no database or
            # registry lock carried across admission.
            await self.owner.allow(self.turn, round_number, call)
        except BaseException:
            if reservation is not None:
                await asyncio.to_thread(reservation.finish, completed=False)
            raise
        if reservation is not None:
            self.reservations[ToolCallIdentity(round_number, call.call_id)] = reservation

    def tool_finished(self, event, input_id):
        super().tool_finished(event, input_id)
        identity, _ = self.calls.require_current(event.tool_call_id, 'Missing completed tool')
        reservation = self.reservations.pop(identity, None)
        if reservation is not None:
            task = asyncio.create_task(asyncio.to_thread(reservation.finish))
            self.completions.add(task)

    async def close(self):
        await super().close()
        if self.completions:
            await asyncio.gather(*tuple(self.completions))
        for reservation in self.reservations.values():
            await asyncio.to_thread(reservation.abandon)
        self.reservations.clear()

    def failure_response(self, error: Exception) -> dict[str, object]:
        return {"ok": False, "error": str(error)}
