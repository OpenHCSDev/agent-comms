"""Read operations share their declaration; callers supply the read authority."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .comms import Comms
from .owner_connection import (
    AttachedResultRuntimeRequest,
    AttachedRuntimeRequestContext,
    OwnerConnection,
    OwnerReading,
    OwnerReplyConnection,
)
from .read_basis import NoDisplaySelection, ReadAuthority
from .tool_declaration import ToolDeclaration
from .tool_request import ThreadReadTool
from .tool_result import ToolResult


class BusRead[Result](ToolDeclaration, ABC):
    """One operation owns its arguments and its read; the boundary supplies authority."""

    @abstractmethod
    def apply(self, comms: Comms, caller: ReadAuthority) -> Result: ...


class UiRead[Result](BusRead[Result], ABC):
    def read(self, comms: Comms) -> Result:
        return self.apply(comms, NoDisplaySelection())


class OwnerLiveRead[Result: ToolResult = ToolResult](BusRead[Result], ThreadReadTool, ABC):
    """Only the selected thread's live owner answers, through its own request queue."""

    def apply(self, comms: Comms, caller: ReadAuthority) -> Result:
        owner = comms.registry.require(caller.subject(self.thread)).incarnation
        request = OwnerReadRuntimeRequest(thread=owner.name, operation=self)
        return self.decode_reply(asyncio.run(OwnerReplyConnection.answer(comms, owner, request)))

    @abstractmethod
    async def owned(self, ctx: AttachedRuntimeRequestContext) -> Result: ...

    @abstractmethod
    def decode_reply(self, raw: object) -> Result: ...


@dataclass(frozen=True, kw_only=True)
class OwnerReadRuntimeRequest(OwnerReading[object], AttachedResultRuntimeRequest):
    operation: OwnerLiveRead

    async def read(self, owner: OwnerConnection) -> object:
        return await owner.request(self.declared_name, operation=self.operation)

    async def result(self, ctx: AttachedRuntimeRequestContext) -> ToolResult:
        return await self.operation.owned(ctx)
