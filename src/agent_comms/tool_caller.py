"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, NoReturn

from .bus_publication import AgentOrigin
from .cli_result import ThreadToolCatalogResult, ToolCatalogResult
from .field_codec import WireForm
from .launch_root import LaunchRoot
from .launch_runtime import CallingThreadUnavailable
from .presentation import MessageNotification, ThreadRecord
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .tools import ToolRequest


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
    def acting_name(self, default: str) -> str: ...

    @abstractmethod
    def from_flag(self) -> tuple[str, ...]:
        """The command line's --from argument naming this caller, if it names a thread."""

    @abstractmethod
    def own_thread(self) -> str:
        """The thread a tool acts on when its call names none."""

    def subject(self, named: str) -> str:
        return named or self.own_thread()

    @abstractmethod
    def may_reconfigure(self, comms: Comms, name: str) -> bool: ...

    @abstractmethod
    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller whose sends direct units refuses a body its directive checks refuse."""

    def origin(self) -> AgentOrigin:
        """Who this caller's sends speak for; an agent speaks only for itself."""
        return AgentOrigin()


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

    def acting_name(self, default: str) -> str:
        return default

    def from_flag(self) -> tuple[str, ...]:
        return ()

    def own_thread(self) -> NoReturn:
        return self.refuse_thread()

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return not comms.registry.status(name).active

    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller with no thread gives no directive."""


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> ToolResult:
        return request.apply(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        presentation = comms.views.thread_presentations((self.name,)).require(self.name)
        return ThreadListing(threads, presentation.sent_notifications)

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult:
        return ThreadToolCatalogResult(
            tools,
            comms.registry.require(self.name).service_tier,
            LaunchRoot.from_path(comms.root).arguments(),
        )

    def acting_name(self, default: str) -> str:
        return self.name

    def from_flag(self) -> tuple[str, ...]:
        return ("--from", self.name)

    def own_thread(self) -> str:
        return self.name

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return (not comms.registry.status(name).active
                or comms.registry.canonical_name(name) == comms.registry.canonical_name(self.name))

    def require_directive(self, comms: Comms, body: str) -> None:
        """A unit's or a script's send is a message, not a directive."""
