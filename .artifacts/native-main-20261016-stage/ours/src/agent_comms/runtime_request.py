"""Runtime protocol, decoding and dispatch context, independent of UI handlers."""
from __future__ import annotations
import asyncio
import os
from .coordinator import Coordination
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self
from .command import Command
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
if TYPE_CHECKING:
    from .runtime import RuntimeServer, SocketClient

@dataclass(frozen=True)
class RuntimeRequestContext:
    server: RuntimeServer
    reader: asyncio.StreamReader
    client: SocketClient
    session_id: str
    name: str

    async def send(self, payload: dict[str, Any]) -> None:
        await self.client.send(payload)

@dataclass(frozen=True, kw_only=True)
class RuntimeRequest(DeclaredFamily, Command, affix="RuntimeRequest", discovery_package="agent_comms.runtime_requests"):
    family_discriminator = "action"
    thread: str

    @classmethod
    def from_wire(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise ValueError("Expected a runtime request object.")
        return cls.from_payload(payload)

    def to_wire(self) -> dict[str, Any]:
        return FieldCodec.encode(self)

    @classmethod
    def proxy_payload(
        cls, thread: str, controller_token: str | None, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        # The owner decodes once; proxies preserve the owner's error envelope.
        return {cls.family_discriminator: FieldCodec.encode(cls), "thread": thread,
                **FieldCodec.encode(parameters)}

    def require_owner(self, snapshot):
        owner = snapshot.require(self.thread)
        if owner.pid != os.getpid() or not snapshot.status(owner.name).running:
            raise RuntimeError("This process no longer owns the thread.")
        return owner

    async def bind(
        self, server: RuntimeServer, reader: asyncio.StreamReader, client: SocketClient
    ) -> RuntimeRequestContext:
        snapshot = await Coordination.run_worker(server.agent._comms.registry.snapshot)
        owner = self.require_owner(snapshot)
        session_id = server.agent.sessions.require_owned_session(owner, snapshot)
        return RuntimeRequestContext(server, reader, client, session_id, owner.name)

class ResultRuntimeRequest(RuntimeRequest):
    async def apply(self, ctx: RuntimeRequestContext) -> None:
        await ctx.send({"result": await self.result(ctx)})

    @abstractmethod
    async def result(self, ctx: RuntimeRequestContext) -> dict[str, Any]: ...
