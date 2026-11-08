"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

import os
import asyncio
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .child_process import (
    ProcessIdentity,
    ProportionalMemory,
    ResidentMemory,
)
from .coordination.coordinator import Coordination
from .errors import RelationViolationError
from .registry.registry_document import RegistryEntry
from .nominal.field_codec import AcpSessionId, FieldCodec
from .transport.owner_requests import (
    OwnerRetiredReply,
    ReadHostResourcesRuntimeRequest,
    ReadOwnerLaunchRuntimeRequest,
    RetainedOwnerLaunch,
    StartOwnerRuntimeRequest,
    StopOwnerRuntimeRequest,
)
from agent_comms.runtime_server import (
    AttachedRuntimeRequestContext,
    RuntimeRequestContext,
    RuntimeServer,
    ServedAttachedResultRuntimeRequest,
    ServedResultRuntimeRequest,
    SocketClient,
)
from .owner_connection import OwnerConnection
from .registry.thread_identity import OwnerIdentity
from .tool_result import DeclaredReply
from .tool_result import ToolResult
from .registry.registry_store import RegistryReadCounts

if TYPE_CHECKING:
    from .registry.threads import Thread


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
                process, owners, len(sys.modules),
                process.memory(), ProportionalMemory.read(Path("/proc") / str(process.pid)).bytes,
                comms.registry.store.cache.read_counts(),
                comms.registry.storage_rule.host_memory_bytes,
                sum(row.resident for row in ResidentMemory.tree(process, Path("/proc")).values()),
                comms.registry.storage_rule.team_memory_budget(owners),
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
