"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity, ProportionalMemory, ResidentMemory
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec
from agent_comms.registry.registry_document import RegistryEntry
from agent_comms.registry.registry_store import RegistryReadCounts
from agent_comms.registry.thread_identity import OwnerIdentity
from agent_comms.transport.owner_connection import OwnerConnection
from agent_comms.transport.owner_requests import (
    OwnerRetiredReply,
    ReadHostResourcesRuntimeRequest,
    ReadOwnerLaunchRuntimeRequest,
    RetainedOwnerLaunch,
    StartOwnerRuntimeRequest,
    StopOwnerRuntimeRequest,
)
from agent_comms.transport.runtime_server import (
    AttachedRuntimeRequestContext,
    RuntimeRequestContext,
    RuntimeServer,
    ServedAttachedResultRuntimeRequest,
    ServedResultRuntimeRequest,
    SocketClient,
)
from agent_comms.transport.tool_result import DeclaredReply, ToolResult

if TYPE_CHECKING:
    from agent_comms.registry.threads import Thread


@dataclass(frozen=True)
class OwnerAttachedReply(DeclaredReply):
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
        return FieldCodec.decode(cls, OwnerConnection.exchange_at(
            root, thread, ReadHostResourcesRuntimeRequest(thread=thread.name),
        ))


class ServedReadHostResourcesRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    ReadHostResourcesRuntimeRequest,
):
    async def result(self, ctx: AttachedRuntimeRequestContext) -> HostResources:
        owners = sum(len(state.executors()) for state in ctx.server.agent.inputs.drained.values())
        def capture() -> HostResources:
            comms = ctx.server.agent._comms
            process = ProcessIdentity.capture(os.getpid())
            return HostResources(
                process=process, owners=owners, modules=len(sys.modules),
                memory=process.memory(), proportional_bytes=ProportionalMemory.read(Path("/proc") / str(process.pid)).bytes,
                registry=comms.registry.store.cache.read_counts(),
                budget_bytes=comms.registry.storage_rule.host_memory_bytes,
                fleet_resident_bytes=sum(row.resident for row in ResidentMemory.tree(process, Path("/proc")).values()),
                fleet_budget_bytes=comms.registry.storage_rule.team_memory_budget(owners),
            )
        return await Coordination.run_worker(capture)


class ServedReadOwnerLaunchRuntimeRequest(
    ServedAttachedResultRuntimeRequest,
    ReadOwnerLaunchRuntimeRequest,
):
    def require_owner(self, entry: RegistryEntry) -> Thread:
        if entry.owner_identity != self.owner:
            raise RelationViolationError("Launch request's owner lease changed")
        return super().require_owner(entry)

    async def result(self, ctx: AttachedRuntimeRequestContext) -> RetainedOwnerLaunch:
        state = ctx.server.agent.inputs.state(ctx.session).owned()
        if state.owner != self.owner:
            raise RelationViolationError("Launch request names another original session")
        return state.configuration.launch


class ServedStartOwnerRuntimeRequest(
    ServedResultRuntimeRequest[RuntimeRequestContext],
    StartOwnerRuntimeRequest,
):
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


class ServedStopOwnerRuntimeRequest(
    ServedResultRuntimeRequest[RuntimeRequestContext],
    StopOwnerRuntimeRequest,
):
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
        session = next(agent.sessions.fenced_sessions(owner.incarnation), AcpSessionId(owner.name))
        return await agent.inputs.state(session).retire_for_stop(
            ctx.server.agent.sessions, self)
