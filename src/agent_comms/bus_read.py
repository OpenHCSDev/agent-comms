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
from typing import TYPE_CHECKING, NoReturn

from .launch_runtime import CallingThreadUnavailable
from .presentation import AllOutcomes
from .tool_declaration import ToolDeclaration
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .owner_connection import RuntimeRequestContext
    from .presentation import OutcomeViewer
    from .runtime import RuntimeConnection
    from .thread_identity import ThreadIncarnation
    from .threads import Thread


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


class OwnerReading[Result](ABC):
    """One exchange with exactly one live owner incarnation over its socket."""

    def admit(self, owner: Thread) -> None:
        """An ordinary owner reading accepts any registered owner of the held incarnation."""

    @abstractmethod
    async def read(self, owner: RuntimeConnection) -> Result: ...


class OwnerLiveRead[Result: ToolResult](OwnerReading[Result], BusRead[Result], ABC):
    """A read only the selected thread's live native owner can answer, from its own queue.

    MCP and the UI both reach the owner; neither answers from its own process, and owner
    loss reaches the caller as the original refusal. The owner runs ``owned`` against its
    own registration and session, never against the public thread selector.
    """

    if TYPE_CHECKING:
        # The composed ThreadReadTool declares the field; an annotation here would shadow it.
        thread: str

    def apply(self, comms: Comms, caller: BusReader) -> Result:
        return self.read_as(comms, caller)

    def read_as(self, comms: Comms, reader: BusReader) -> Result:
        from .runtime import OwnerReplyConnection

        owner = comms.registry.require(reader.subject(self.thread)).incarnation
        return asyncio.run(OwnerReplyConnection.answer(comms, owner, self))

    def admit(self, owner: Thread) -> None:
        owner.execution.require_native()

    async def read(self, owner: RuntimeConnection) -> Result:
        from .runtime_requests import OwnerReadRuntimeRequest

        return self.decode_reply(
            await owner.request(OwnerReadRuntimeRequest.declared_name, operation=self))

    @abstractmethod
    async def owned(self, ctx: RuntimeRequestContext) -> Result:
        """The answer computed inside the owner, under its own context."""

    @abstractmethod
    def decode_reply(self, raw: object) -> Result: ...
