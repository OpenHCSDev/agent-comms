"""One declaration per bus read: the operation owns its arguments and its read once.

A ``BusRead`` member is the operation. Each boundary that serves it keeps only what is
its own: the UI its root, request id and refusals (``ReadRequest``), MCP its caller,
exposure and reply (``ToolRequest``). Neither boundary restates the read, and the
reader's authority reaches the operation as a ``BusReader`` the boundary supplies,
never as an argument the caller may choose.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from .launch_runtime import CallingThreadUnavailable
from .owner_connection import (
    OwnerConnection, OwnerReading, OwnerReplyConnection, ResultRuntimeRequest, RuntimeRequestContext,
)
from .presentation import AllOutcomes
from .tool_declaration import ToolDeclaration
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .presentation import OutcomeViewer


class BusReader(ABC):
    """Whose authority a read runs under; each boundary supplies its own reader."""

    @abstractmethod
    def outcome_viewer(self, comms: Comms) -> OutcomeViewer:
        """Whose recipient outcomes this reader may read."""

    def subject(self, named: str) -> str:
        return named or self.own_thread()

    def own_thread(self) -> str:
        """The thread a read targets when its call names none; a reader with no thread has none."""
        return self.refuse_thread()

    def refuse_thread(self) -> NoReturn:
        raise CallingThreadUnavailable("This tool requires a thread caller.")


class OperatorOutcomes:
    """A reader speaking for the owner reads every recipient outcome, as the owner's UI does."""

    __slots__ = ()

    def outcome_viewer(self, comms: Comms) -> OutcomeViewer:
        return AllOutcomes()


class OperatorReader(OperatorOutcomes, BusReader):
    """The owner's UI: it observes the whole bus as the operator."""

    __slots__ = ()


class BusRead[Result](ToolDeclaration, ABC):
    """A read of Core's stores, declared once with its arguments; boundaries derive it."""

    @abstractmethod
    def read_as(self, comms: Comms, reader: BusReader) -> Result:
        """The read under the reader's authority."""


class UiRead[Result](BusRead[Result], ABC):
    """The UI serves this read as the operator; its envelope (root, request id, refusal)
    stays on ``ReadRequest``."""

    def read(self, comms: Comms) -> Result:
        return self.read_as(comms, OperatorReader())


class OwnerLiveRead[Result: ToolResult = ToolResult](BusRead[Result], ABC):
    """A read only the selected thread's live native owner can answer, from its own queue.

    MCP and the UI both reach the owner through ``OwnerReadRuntimeRequest``; neither answers
    from its own process, and owner loss reaches the caller as the original refusal. The
    owner runs ``owned`` against its own registration and session, never against the public
    thread selector.
    """

    if TYPE_CHECKING:
        # The composed ThreadReadTool declares the field; an annotation here would shadow it.
        thread: str

    def apply(self, comms: Comms, caller: BusReader) -> Result:
        return self.read_as(comms, caller)

    def read_as(self, comms: Comms, reader: BusReader) -> Result:
        owner = comms.registry.require(reader.subject(self.thread)).incarnation
        request = OwnerReadRuntimeRequest(thread=owner.name, operation=self)
        return self.decode_reply(asyncio.run(OwnerReplyConnection.answer(comms, owner, request)))

    @abstractmethod
    async def owned(self, ctx: RuntimeRequestContext) -> Result:
        """The answer computed inside the owner, under its own context."""

    @abstractmethod
    def decode_reply(self, raw: object) -> Result: ...


@dataclass(frozen=True, kw_only=True)
class OwnerReadRuntimeRequest(OwnerReading[object], ResultRuntimeRequest):
    """The one runtime envelope for every owner-live read: sent once to the held owner, and
    answered there by the operation under the owner's context."""

    operation: OwnerLiveRead

    async def read(self, owner: OwnerConnection) -> object:
        return await owner.request(self.declared_name, operation=self.operation)

    async def result(self, ctx: RuntimeRequestContext) -> ToolResult:
        return await self.operation.owned(ctx)
