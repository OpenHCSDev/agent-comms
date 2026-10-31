"""Normal Pi coding tools authorized by the selected channel turn's existing owner.

Pi owns actual read/bash/edit/write execution. This module owns the cooperative
pre-tool claim check, using the bus's existing claims and native input ledger.
Shell is not a filesystem sandbox; shell work must respect the injected claims.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordinator import Coordination

from .comms import Comms
from .coordination_database import CoordinationStore
from .envelope_claim_transitions import (
    ClaimOwner,
    FileClaimPath,
    WakeClaim,
)
from .field_codec import ClaimId, FieldCodec, InputId, MalformedRecord
from .lifecycle_capability import Executing
from .native_arguments import NativeArguments
from .native_runtime_input import NativeRuntimeInput
from .native_tool_call import CodingCall, NativeToolMode, ToolNotExecuting
from .native_tool_transport import GatedToolTransport, ToolListener, UnboundToolTransport
from .native_tools import NativeToolDeclaration
from .pi_payloads import StorageDenialToolDetails
from .pi_rule_gate import PiRuleGate
from .selected_claim import (
    publish_selected_resource_claim,
    verify_selected_wake,
)
from .selected_tool_broker import (
    consume_selected_slot,
    verify_sent_full_input,
)
from .shell_effects import StorageDenied
from .storage_projection import StorageProjection
from .storage_room import NoRoomNeeded, RoomReservation
from .threads import Thread
from .tool_denial import SelectedToolDenied
from .turn_goal_permission import TurnGoalPermission

from .reservation_rules import ReservationRule, ReservationViolationError
from .turn_input_source import TurnInputSource
from .reservation_rules import RuleCheck
from .goals import GoalDeclaration
from .goal_waits import GoalWait
if TYPE_CHECKING:
    from .backend import TurnSession
    from .coordination_tables.assignments import WakeAssignment
    from .pi_payloads import PiToolResult


@dataclass(frozen=True, kw_only=True)
class OrdinaryContextCheck(RuleCheck):
    source: TurnInputSource
    owner: Thread
    goal: GoalDeclaration
    wait: GoalWait
    comms: Comms


class OrdinaryGoalRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "The input's captured goal permission no longer applies."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.goal_permission.allows(check.goal)

    @classmethod
    def require(cls, permission: TurnGoalPermission, current: GoalDeclaration) -> None:
        if not permission.allows(current):
            raise SelectedToolDenied(str(ReservationViolationError(cls())))


class OrdinaryDependencyRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "The original dependency wait was removed or replaced."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.dependency_current(check.wait)


class OrdinaryWaitRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "The current dependency wait does not authorize this input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_wait(
            check.wait, check.comms, check.owner.incarnation.require_recorded()
        )


class OrdinaryGoalInputRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "An active goal does not authorize this routed input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_goal_input(check.goal)


@dataclass(frozen=True, kw_only=True)
class CodingToolOwner(ABC):
    comms: Comms
    thread: Thread
    claims: dict[str, ClaimOwner] = field(default_factory=dict)
    check_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    @property
    def owner_name(self) -> str:
        return self.thread.name

    @property
    @abstractmethod
    def goal_permission(self) -> TurnGoalPermission: ...

    async def reserve_room(self, call: CodingCall) -> RoomReservation:
        registry = self.comms.registry
        thread = self.thread
        projection = StorageProjection(registry.storage_rule, thread, self.comms)
        effects = await asyncio.to_thread(projection.check_call, call)
        work = tuple(dict.fromkeys(kind for effect in effects for kind in effect.work))
        if work:
            return await registry.storage_rule.wait_for_room(
                work, thread, self.comms.root)
        return NoRoomNeeded()

    async def allow(self, turn, round_number: int, call: CodingCall) -> RoomReservation:
        def check(store: Coordination) -> None:
            snapshot = self.comms.registry.snapshot()
            OrdinaryGoalRule.require(self.goal_permission, snapshot.require(self.owner_name).goal)
            self.verify(store, turn, snapshot)
            slot = call.slot(turn.command.input_id, round_number)
            consume_selected_slot(store, slot, call.call_id)
            resource = call.allowed_claim()
            if resource is not None:
                canonical = resource.normalized(self.thread.worktree.path)
                self.claims[canonical] = self.claim_resource(
                    store, turn, replace(resource, resource=Path(canonical)),
                    FieldCodec.decode(ClaimId, slot))

        reservation = await self.reserve_room(call)
        async with reservation.checking(), self.check_lock:
            await Coordination.run_async(CoordinationStore.path_in(self.comms.root), check)
        return reservation

    def tool_completed(self, call: CodingCall, result: PiToolResult) -> None:
        """The checked terminal shares its actual publication with this turn's grant."""
        call.published_goal(result, self.goal_permission)

    @abstractmethod
    def verify(self, store, turn, snapshot): ...

    @abstractmethod
    def claim_resource(self, store: Coordination, turn: TurnSession,
                       resource: FileClaimPath, slot: ClaimId) -> ClaimOwner: ...

    def for_original(self, assignment: WakeAssignment, operation_id: ClaimId) -> CodingToolOwner:
        raise SelectedToolDenied("An ordinary input has no selected original")


@dataclass(frozen=True, slots=True)
class SelectedCodingToolOwner(CodingToolOwner):
    store: Coordination
    resource_scope: WakeClaim
    session_dir: Path
    input_id: InputId
    goal_permission: TurnGoalPermission = field()

    def for_original(self, assignment: WakeAssignment, operation_id: ClaimId) -> CodingToolOwner:
        """Project the same native grant onto an actually included original."""
        with self.store.session.read():
            native = NativeRuntimeInput.one(self.store.session._connection, input_id=self.input_id)
            if native is None or assignment.assignment_id not in native.execution.source_assignment_ids(
                self.store.session._connection, self.input_id
            ):
                raise SelectedToolDenied("Tool original is absent from the reserved native batch")
        return replace(self, resource_scope=replace(
            self.resource_scope,
            source_seq=assignment.wire_seq,
            source_message_id=assignment.message_id,
            wake_assignment_id=assignment.assignment_id,
            wake_revision=assignment.revision,
            operation_id=operation_id,
        ))

    def verify(self, store, turn, snapshot):
        if self.input_id != turn.command.input_id:
            raise SelectedToolDenied("Native tool input changed")
        verify_sent_full_input(store, self.resource_scope, self.owner_name, self.input_id)
        verify_selected_wake(self.comms, store, self.resource_scope, self.owner_name)

    def claim_resource(self, store: Coordination, turn: TurnSession,
                       resource: FileClaimPath, slot: ClaimId) -> ClaimOwner:
        scope = replace(self.resource_scope, operation_id=slot)
        return publish_selected_resource_claim(self.comms, store, scope, self.owner_name, resource)



@dataclass(frozen=True)
class CodingToolMode(NativeToolMode):
    owner: CodingToolOwner

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        declarations = NativeToolDeclaration.members_with(NativeToolDeclaration)
        arguments = ("--tools", ",".join(tool.declared_name for tool in declarations),
                     *NativeToolDeclaration.extension_arguments(package))
        return self.owner.comms.threads.native_launch_arguments(
            self.owner.thread, NativeArguments.parse(arguments), package,
        )

    async def open_transport(self, turn, ownership) -> UnboundToolTransport:
        return await CodingToolSocket.open(
            turn, ownership, self.owner, PiRuleGate.of(self.owner.comms, self.owner.thread))

@dataclass(frozen=True)
class CodingToolRequest:
    """The native coding socket's object-valued call, not a Codex rollout item."""

    token: str = field(repr=False)
    call_id: str
    name: str
    arguments: object

    def authenticate(self, expected_token: str) -> CodingCall:
        if self.token != expected_token:
            raise SelectedToolDenied("Coding request is not authenticated")
        return CodingCall.decode(self.call_id, self.name, self.arguments)


class CodingToolSocket(GatedToolTransport[CodingCall]):
    """Multi-call native policy on the same peer-authenticated owner transport."""

    def __init__(self, listener: ToolListener, owner: CodingToolOwner, gate: PiRuleGate,
                 *, pid: int) -> None:
        super().__init__(listener, gate, pid=pid)
        self.owner = owner
        self.completions = set()

    def decode_call(self, call_id: object, name: object, arguments: object) -> CodingCall:
        return CodingCall.decode(call_id, name, arguments)

    def decode_request(self, raw: bytes) -> CodingCall:
        if not raw.endswith(b"\n") or len(raw) > self.max_request:
            raise SelectedToolDenied("Coding request incomplete or oversized")
        try:
            request = FieldCodec.decode(CodingToolRequest, FieldCodec.parse(raw))
        except MalformedRecord as error:
            raise SelectedToolDenied("Coding request is not authenticated") from error
        return request.authenticate(self.listener.token)

    async def allow(self, round_number: int, call: CodingCall) -> None:
        # The owner rechecks authority after its unlocked room wait. The
        # transport holds the returned reservation for this exact tool call.
        reservation = await self.owner.allow(self.turn, round_number, call)
        async with reservation.checking():
            call.progress.observation.require(Executing, ToolNotExecuting())
            call.progress.room = reservation

    def tool_finished(self, event, input_id):
        super().tool_finished(event, input_id)
        _, call = self.calls.require_current(
            FieldCodec.encode(event.tool_call_id), 'Missing completed tool'
        )
        if not event.is_error.value_or(False):
            self.owner.tool_completed(call, event.result)
            call.completed_under(self.rule_gate)
        task = asyncio.create_task(call.progress.finish_room())
        self.completions.add(task)

    async def _drain_calls(self):
        if self.completions:
            await asyncio.gather(*tuple(self.completions))
        for call in self.calls.values():
            await asyncio.to_thread(call.progress.room.abandon)

    async def handle_request(self, raw: bytes) -> dict[str, object]:
        try:
            return await super().handle_request(raw)
        except StorageDenied as denial:
            return {"ok": False, "error": str(denial),
                    "details": StorageDenialToolDetails(str(denial)).to_wire()}

    @staticmethod
    def failure_response(error: Exception) -> dict[str, object]:
        return {"ok": False, "error": str(error)}
