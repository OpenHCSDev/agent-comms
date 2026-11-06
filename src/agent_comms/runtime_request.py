"""The runtime request family: what a request binds to and how its result goes back.

The server that runs these lives in runtime_server; it carries the ACP permission broker, so
declaring a request never loads the ACP schema."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.owner_connection import AnyOwner, NoControllerToken, OwnerConnection, RequestedOwner
from .command import Command
from .coordination.coordinator import Coordination
from .errors import RelationViolationError
from .nominal.declared_family import DeclaredFamily
from .nominal.field_codec import AcpSessionId, FieldCodec, WireOmitDefault
from .owner_protocol import OwnerLine, OwnerResult
from .storage.maintenance_barrier import MaintenanceHolder, NoMaintenanceHolder
from .thread_identity import ThreadIncarnation
from .tool_result import DeclaredReply

if TYPE_CHECKING:
    from .registry_document import RegistryEntry
    from .runtime_server import RuntimeServer, SocketClient
    from .threads import Thread


@dataclass(frozen=True)
class RuntimeRequestContext:
    server: RuntimeServer
    reader: asyncio.StreamReader
    client: SocketClient
    name: str

    async def send(self, line: OwnerLine) -> None:
        await self.client.send(line)


@dataclass(frozen=True)
class AttachedRuntimeRequestContext(RuntimeRequestContext):
    session: AcpSessionId


@dataclass(frozen=True, kw_only=True)
class RuntimeRequest[Ctx: RuntimeRequestContext](
    DeclaredFamily, Command[Ctx, Awaitable[None]], affix="RuntimeRequest"
):
    family_discriminator: ClassVar[str] = "action"
    thread: str
    holder: Annotated[MaintenanceHolder, WireOmitDefault()] = NoMaintenanceHolder()
    captured: Annotated[RequestedOwner, WireOmitDefault()] = AnyOwner()

    @classmethod
    def proxy_payload(
        cls, owner: ThreadIncarnation, controller_token: str | NoControllerToken,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        return OwnerConnection.wire_request(cls.declared_name, owner, parameters)

    @abstractmethod
    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient,
    ) -> Ctx: ...


class AttachedRuntimeRequest(RuntimeRequest[AttachedRuntimeRequestContext]):
    def require_owner(self, entry: RegistryEntry) -> Thread:
        owner = entry.thread
        if not owner.process_identity.runs_here() or not entry.status.running:
            raise RelationViolationError("This process no longer owns the thread.")
        self.captured.require(owner)
        return owner

    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient,
    ) -> AttachedRuntimeRequestContext:
        entry = (await Coordination.run_worker(partial(
            server.agent._comms.registry.entry, self.thread,
        ))).entry
        owner = self.require_owner(entry)
        session_id = server.agent.sessions.require_owned_session(entry)
        return AttachedRuntimeRequestContext(server, reader, client, owner.name, session_id)


class ResultRuntimeRequest[Ctx: RuntimeRequestContext](RuntimeRequest[Ctx]):
    async def apply(self, ctx: Ctx) -> None:
        reply = await self.result(ctx)
        await ctx.send(OwnerResult(result=FieldCodec.encode(reply)))

    @abstractmethod
    async def result(self, ctx: Ctx) -> DeclaredReply: ...


class AttachedResultRuntimeRequest(
    ResultRuntimeRequest[AttachedRuntimeRequestContext], AttachedRuntimeRequest
):
    pass
