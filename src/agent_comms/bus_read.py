"""Read operations share their declaration; callers supply the read authority."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .comms import Comms
from .owner_connection import (
    OwnerConnection, OwnerReading, OwnerReplyConnection, ResultRuntimeRequest, RuntimeRequestContext,
)
from .read_basis import DisplayCut, NoDisplaySelection, OutcomeViewer, ReadAuthority
from .registration import Registration
from .tool_declaration import ToolDeclaration
from .tool_request import ThreadReadTool
from .tool_result import ToolResult


@dataclass(frozen=True)
class OperatorReader(ReadAuthority):
    """An operator read is scoped by its actual painted display, or is unscoped."""

    display: DisplayCut = field(default_factory=NoDisplaySelection)

    def outcome_viewer(self, registry: Registration) -> OutcomeViewer:
        return self.display.outcome_viewer(registry)


class BusRead[Result](ToolDeclaration, ABC):
    """One operation owns its arguments and its read; the boundary supplies authority."""

    def apply(self, comms: Comms, caller: ReadAuthority) -> Result:
        return self.read_as(comms, caller)

    @abstractmethod
    def read_as(self, comms: Comms, reader: ReadAuthority) -> Result: ...


class UiRead[Result](BusRead[Result], ABC):
    def read(self, comms: Comms) -> Result:
        return self.read_as(comms, OperatorReader())


class OwnerLiveRead[Result: ToolResult = ToolResult](BusRead[Result], ThreadReadTool, ABC):
    """Only the selected thread's live owner answers, through its own request queue."""

    def read_as(self, comms: Comms, reader: ReadAuthority) -> Result:
        owner = comms.registry.require(reader.subject(self.thread)).incarnation
        request = OwnerReadRuntimeRequest(thread=owner.name, operation=self)
        return self.decode_reply(asyncio.run(OwnerReplyConnection.answer(comms, owner, request)))

    @abstractmethod
    async def owned(self, ctx: RuntimeRequestContext) -> Result: ...

    @abstractmethod
    def decode_reply(self, raw: object) -> Result: ...


@dataclass(frozen=True, kw_only=True)
class OwnerReadRuntimeRequest(OwnerReading[object], ResultRuntimeRequest):
    operation: OwnerLiveRead

    async def read(self, owner: OwnerConnection) -> object:
        return await owner.request(self.declared_name, operation=self.operation)

    async def result(self, ctx: RuntimeRequestContext) -> ToolResult:
        return await self.operation.owned(ctx)
