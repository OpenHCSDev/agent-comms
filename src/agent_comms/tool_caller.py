"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, NoReturn

from .cli_result import ToolCatalogResult, ThreadToolCatalogResult
from .launch_runtime import CallingThreadUnavailable
from .field_codec import WireForm
from .presentation import MessageNotification, ThreadRecord
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .messages import Message
    from .tools import ToolRequest
    from .todos import OrderVerdict, Todo


@dataclass(frozen=True)
class ThreadListing(ToolResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]
    sent_notifications: tuple[MessageNotification, ...]


class ToolCaller(ABC):
    @classmethod
    def from_actor(cls, actor: str | None) -> ToolCaller:
        """Each entry boundary (CLI --from, context action) names its actor explicitly."""
        return ThreadCaller(actor) if actor else NoThreadCaller()

    @abstractmethod
    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult: ...

    @abstractmethod
    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing: ...

    @abstractmethod
    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult: ...

    @abstractmethod
    def subject(self, named: str) -> str: ...

    @abstractmethod
    def may_reconfigure(self, comms: Comms, name: str) -> bool: ...

    @abstractmethod
    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller whose sends direct units refuses a body its directive checks refuse."""

    @abstractmethod
    def sent(self, comms: Comms, message: Message) -> None:
        """What a message this caller sent records beyond the bus."""


@dataclass(frozen=True)
class NoThreadCaller(ToolCaller):
    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult:
        return request.apply_without_thread(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        return ThreadListing(threads, ())

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult:
        return ToolCatalogResult(tools)

    def refuse_thread(self) -> NoReturn:
        raise CallingThreadUnavailable("This tool requires a thread caller.")

    def subject(self, named: str) -> str:
        if not named:
            return self.refuse_thread()
        return named

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return not comms.registry.status(name).active

    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller with no thread gives no directive."""

    def sent(self, comms: Comms, message: Message) -> None:
        """A message sent without a calling thread records nothing more."""


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def judged(self, comms: Comms, problem: str, verdict: OrderVerdict) -> Todo:
        return comms.goals.change_problem(problem, verdict, self.name)

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> ToolResult:
        return request.apply(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        presentation = comms.views.thread_presentations((self.name,)).require(self.name)
        return ThreadListing(threads, presentation.sent_notifications)

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult:
        return ThreadToolCatalogResult(tools, comms.registry.require(self.name).service_tier)

    def subject(self, named: str) -> str:
        return named or self.name

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return (not comms.registry.status(name).active
                or comms.registry.canonical_name(name) == comms.registry.canonical_name(self.name))

    def require_directive(self, comms: Comms, body: str) -> None:
        """A unit's or a script's send is a message, not a directive."""

    def sent(self, comms: Comms, message: Message) -> None:
        """An agent's message is only the bus record."""
