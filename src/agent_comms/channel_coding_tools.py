"""Normal Pi coding tools authorized by the selected channel turn's existing owner.

Pi owns actual read/bash/edit/write execution. This module owns the cooperative
pre-tool claim check, using the bus's existing claims and native input ledger.
Shell is not a filesystem sandbox; shell work must respect the injected claims.
"""

from __future__ import annotations

import asyncio
import hashlib
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import InitVar, dataclass, field, replace
from pathlib import Path
from typing import Never

from agent_comms.coordinator import Coordination

from .selected_claim import (
    publish_selected_resource_claim,
    verify_selected_wake,
)
from .comms import Comms
from .field_codec import FieldCodec, MalformedRecord
from .envelope_claim_transitions import (
    ClaimOwner,
    WakeClaim,
)
from .native_tool_call import NativeToolCall, NativeToolMode, SelectedToolDenied, UncertainClaim
from .native_arguments import NativeArguments
from .native_tools import NativeExecutableTool
from .selected_tool_broker import (
    OwnerToolSocket,
    consume_selected_slot,
    record_selected_terminal,
    verify_sent_full_input,
)


@dataclass
class CodingCall(NativeToolCall):
    @abstractmethod
    def require_tool(self) -> NativeExecutableTool | type[NativeExecutableTool]: ...

    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None:
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
        ):
            raise SelectedToolDenied("Malformed native coding call identity")
        try:
            return NativeExecutableTool.from_call(name, arguments, call_id=call_id)
        except MalformedRecord as error:
            return RefusedCodingCall(call_id, name, arguments, str(error))

    def slot(self, input_id: str, round_number: int) -> str:
        identity = f"{input_id}\0{round_number}\0{self.call_id}"
        return hashlib.sha256(identity.encode()).hexdigest()[:32]


@dataclass
class ExecutableCodingCall(CodingCall):
    tool: NativeExecutableTool | type[NativeExecutableTool]

    @property
    def name(self) -> str:
        return self.tool.declared_name

    def require_tool(self) -> NativeExecutableTool | type[NativeExecutableTool]:
        return self.tool


@dataclass
class ArgumentRefusal(UncertainClaim):
    reason: str

    async def consume(
        self, call: NativeToolCall, action: Callable[[], Awaitable[None]],
    ) -> None:
        raise SelectedToolDenied(self.reason)


@dataclass
class RefusedCodingCall(CodingCall):
    """Invalid arguments cannot execute, but still own real start/error events."""

    tool_name: str
    arguments: object
    reason: InitVar[str]

    def __post_init__(self, reason: str) -> None:
        self.claim_scope = ArgumentRefusal(reason)

    @property
    def name(self) -> str:
        return self.tool_name

    def require_tool(self) -> Never:
        raise SelectedToolDenied("Refused native coding arguments cannot execute")

    def correlate(self, candidate: NativeToolCall) -> None:
        self.reject_correlated(candidate)

    def reject_correlated(self, existing: NativeToolCall) -> None:
        # Pi may normalize arguments before its socket arrives. Whichever
        # arrives first keeps the same lifecycle object, never permission.
        if (self.call_id, self.name) != (existing.call_id, existing.name):
            raise SelectedToolDenied("Refused coding call identity changed")
        existing.claim_scope.refuse(existing, self.claim_scope)


@dataclass
class CoordinationCall(ExecutableCodingCall):
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
            tool = call.require_tool()
            try:
                effects = tool.storage_effects(Path(owner.worktree))
                if effects:
                    self.comms.registry.decide_storage(self.owner_name, effects)
            except SelectedToolDenied as error:
                raise SelectedToolDenied(f'{error}\n{self.comms.registry.storage_rule.render(owner.storage_roots)}') from error
            resource = tool.claim
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
        arguments = ("--tools", ",".join(tool.declared_name for tool in declarations),
                     *(argument for extension in extensions for argument in ("-e", str(extension))))
        thread = self.owner.comms.registry.require(self.owner.owner_name)
        return thread.native_launch_arguments(NativeArguments.parse(arguments), package)

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
        value = FieldCodec.parse(raw)
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
