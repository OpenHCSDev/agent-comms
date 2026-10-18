"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from pydantic import JsonValue

from .cli_result import CliResult
from .launch_runtime import CallingThreadUnavailable
from .pi_vocabulary import ServiceTier
from .presentation import MessageNotification
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .tools import ToolRequest


@dataclass(frozen=True)
class ThreadListing(ToolResult):
    threads: tuple[dict[str, JsonValue], ...]
    sent_notifications: tuple[MessageNotification, ...]


@dataclass(frozen=True)
class ToolCatalogResult(CliResult):
    tools: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ThreadToolCatalogResult(ToolCatalogResult):
    service_tier: ServiceTier


class ToolCaller(ABC):
    @classmethod
    def from_actor(cls, actor: str | None) -> ToolCaller:
        """Each entry boundary (CLI --from, context action) names its actor explicitly."""
        return ThreadCaller(actor) if actor else NoThreadCaller()

    @abstractmethod
    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult: ...

    @abstractmethod
    def listing(self, comms: Comms, threads: tuple[dict[str, JsonValue], ...]) -> ThreadListing: ...

    @abstractmethod
    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult: ...

    @abstractmethod
    def subject(self, named: str) -> str: ...

    @abstractmethod
    def may_reconfigure(self, comms: Comms, name: str) -> bool: ...


@dataclass(frozen=True)
class NoThreadCaller(ToolCaller):
    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult:
        return request.apply_without_thread(comms, self)

    def listing(self, comms: Comms, threads: tuple[dict[str, JsonValue], ...]) -> ThreadListing:
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


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> ToolResult:
        return request.apply(comms, self)

    def listing(self, comms: Comms, threads: tuple[dict[str, JsonValue], ...]) -> ThreadListing:
        presentation = comms.views.thread_presentations((self.name,)).require(self.name)
        return ThreadListing(threads, presentation.sent_notifications)

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ThreadToolCatalogResult:
        return ThreadToolCatalogResult(tools, comms.registry.require(self.name).service_tier)

    def subject(self, named: str) -> str:
        return named or self.name

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return (not comms.registry.status(name).active
                or comms.registry.canonical_name(name) == comms.registry.canonical_name(self.name))
