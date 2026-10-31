"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

import os
import asyncio
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .child_process import Platform, ProcessIdentity, ProportionalMemory, ResidentMemory
from .coordinator import Coordination
from .errors import RelationViolationError
from . import launch_runtime
from .native_arguments import ConfiguredBinary, InheritedArguments, LaunchArguments, LaunchBinary, NativeArguments
from .registry_document import RegistryEntry, RegistrySnapshot
from .field_codec import AcpSessionId, FieldCodec
from agent_comms.runtime_server import AttachedResultRuntimeRequest, AttachedRuntimeRequestContext, ResultRuntimeRequest, RuntimeRequestContext
from .owner_connection import OwnerConnection
from .thread_identity import ClaimedIdentity, OwnerIdentity
from .tool_result import DeclaredReply
from .tool_result import ToolResult
from .registry_store import RegistryReadCounts

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerRestartSelection
    from .runtime_server import RuntimeServer, SocketClient
    from .threads import Thread
    from .session_lifecycle import SessionLifecycle
    from .input_drain import OwnerSession


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch(DeclaredReply):
    """Original session configuration, not ownership or permission to start work."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    configuration: launch_runtime.RestartEnvironment = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", launch_runtime.RestartEnvironment.inherit(self.environment))

    @classmethod
    def configured(
        cls, binary: LaunchBinary = ConfiguredBinary(),
        arguments: LaunchArguments = InheritedArguments(),
    ) -> RetainedOwnerLaunch:
        """Capture a process ingress once, before its configuration is shared by reference."""
        environment = dict(os.environ)
        environment[NativeArguments.binary_key] = binary.configured(environment)
        arguments.apply(environment)
        return cls(ProcessIdentity.capture(os.getpid()),
                   str(launch_runtime.LaunchRuntime.running().interpreter), environment)

    @classmethod
    def capture(
        cls, selection: OwnerRestartSelection, snapshot: RegistrySnapshot, root: Path,
    ) -> RetainedOwnerLaunch:
        thread = selection.require_current(snapshot)
        process = selection.process
        platform = Platform.current()
        platform.require(process)
        result = FieldCodec.decode(cls, OwnerConnection.exchange(
            root, process, ReadOwnerLaunchRuntimeRequest(thread=thread.name, owner=selection.identity),
        ))
        if result.process != process or result.interpreter != selection.interpreter:
            raise RelationViolationError("Selected source owner interpreter changed")
        platform.require(process)
        route = launch_runtime.ThreadLaunch.current(result.environment).name
        if not result.environment.get(NativeArguments.binary_key):
            raise RelationViolationError("Owner launch configuration cannot be verified")
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        result.arguments  # Decode/validate malformed shell arguments before any fence.
        return result

    @property
    def binary(self) -> str:
        return self.environment[NativeArguments.binary_key]

    @property
    def arguments(self) -> tuple[str, ...]:
        return InheritedArguments().configured(self.environment).argv


@dataclass(frozen=True)
class OwnerAttachedReply(DeclaredReply):
    owner: OwnerIdentity


@dataclass(frozen=True)
class OwnerRetiredReply(DeclaredReply):
    owner: OwnerIdentity


@dataclass(frozen=True)
class HostResources(ToolResult):
    """The shared control host; native model children have independent allocations."""

    process: ProcessIdentity
    owners: int
    modules: int
    memory: ResidentMemory
    proportional_bytes: int
    registry: RegistryReadCounts
    budget_bytes: int
    fleet_resident_bytes: int
    fleet_budget_bytes: int

    @classmethod
    def read(cls, root: Path, thread: Thread) -> HostResources:
        """The host's own status read, asked through one thread it owns."""
        return FieldCodec.decode(cls, OwnerConnection.exchange(
            root, thread.require_process(), ReadHostResourcesRuntimeRequest(thread=thread.name),
        ))


@dataclass(frozen=True, kw_only=True)
class ReadHostResourcesRuntimeRequest(AttachedResultRuntimeRequest):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> HostResources:
        owners = sum(len(state.executors()) for state in ctx.server.agent.inputs.drained.values())
        def capture() -> HostResources:
            comms = ctx.server.agent._comms
            process = ProcessIdentity.capture(os.getpid())
            return HostResources(
                process, owners, len(sys.modules),
                process.memory(), ProportionalMemory.read(Path("/proc") / str(process.pid)).bytes,
                comms.registry.store.cache.read_counts(),
                comms.registry.storage_rule.host_memory_bytes,
                sum(row.resident for row in ResidentMemory.tree(process, Path("/proc")).values()),
                comms.registry.storage_rule.fleet_memory_budget(owners),
            )
        return await Coordination.run_worker(capture)


@dataclass(frozen=True, kw_only=True)
class ReadOwnerLaunchRuntimeRequest(AttachedResultRuntimeRequest):
    owner: OwnerIdentity

    def require_owner(self, entry: RegistryEntry) -> Thread:
        if entry.owner_identity != self.owner:
            raise RelationViolationError("Launch request's owner lease changed")
        return super().require_owner(entry)

    async def result(self, ctx: AttachedRuntimeRequestContext) -> RetainedOwnerLaunch:
        state = ctx.server.agent.inputs.state(ctx.session).owned()
        if state.owner != self.owner:
            raise RelationViolationError("Launch request names another original session")
        return state.configuration.launch


@dataclass(frozen=True, kw_only=True)
class StartOwnerRuntimeRequest(ResultRuntimeRequest[RuntimeRequestContext]):
    launch: RetainedOwnerLaunch
    owner: OwnerIdentity

    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient,
    ) -> RuntimeRequestContext:
        if not self.launch.process.runs_here():
            raise RelationViolationError("Start request belongs to another host process")
        if self.thread != self.owner.incarnation.name:
            raise RelationViolationError("Start request names another owner")
        return RuntimeRequestContext(server, reader, client, self.thread)

    async def result(self, ctx: RuntimeRequestContext) -> OwnerAttachedReply:
        await ctx.server.agent.sessions.start_owner(self.launch, self.owner)
        return OwnerAttachedReply(self.owner)


@dataclass(frozen=True, kw_only=True)
class StopOwnerRuntimeRequest(ResultRuntimeRequest[RuntimeRequestContext]):
    owner: OwnerIdentity
    claim: ClaimedIdentity

    def require_owner(self, entry: RegistryEntry) -> Thread:
        if entry.owner_identity != self.owner:
            raise RelationViolationError("Stop request's fenced owner changed")
        entry.status.require_stopped()
        if not entry.thread.process_identity.runs_here():
            raise RelationViolationError("Stop request belongs to another host process")
        return entry.thread

    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient,
    ) -> RuntimeRequestContext:
        # A failed start reserved an owner but never published an ACP attachment.
        return RuntimeRequestContext(server, reader, client, self.thread)

    async def result(self, ctx: RuntimeRequestContext) -> OwnerRetiredReply:
        agent = ctx.server.agent
        entry = (await Coordination.run_worker(partial(
            agent._comms.registry.entry, self.thread,
        ))).entry
        owner = self.require_owner(entry)
        session = next(agent.sessions.owned_sessions(entry), AcpSessionId(owner.name))
        return await agent.inputs.state(session).retire_for_stop(
            ctx.server.agent.sessions, self)

    async def retire(self, sessions: SessionLifecycle, state: OwnerSession) -> OwnerRetiredReply:
        if state.owner.incarnation != self.owner.incarnation or state.owner.generation >= self.owner.generation:
            raise RelationViolationError("Stop request does not fence this original session")
        await sessions.retire_owner(state, self.owner, self.claim)
        return OwnerRetiredReply(state.owner)
