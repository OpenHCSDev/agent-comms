"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, NoReturn

from .bus_publication import AgentOrigin
from .cli_result import ThreadToolCatalogResult, ToolCatalogResult
from .errors import ToolRefusedError
from .field_codec import WireForm
from .launch_root import LaunchRoot
from .launch_runtime import CallingThreadUnavailable
from .presentation import MessageNotification, ThreadRecord
from .thread_identity import ThreadIncarnation
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .registration import Registration
    from .head_review import HeadReviewOrder, PullRequestHead
    from .threads import Thread
    from .tool_request import ToolRequest


@dataclass(frozen=True)
class ThreadListing(ToolResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]
    sent_notifications: tuple[MessageNotification, ...]


class ToolCaller(ABC):
    def acting_caller(self, grant: ToolCaller) -> ToolCaller:
        return self

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

    def commanding(self, comms: Comms, commander: str) -> None:
        """An ordinary caller needs no commander-child registration."""
        return None

    def retire(self, comms: Comms) -> None:
        raise ToolRefusedError('Only a commander fork can retire through this verb.')

    @abstractmethod
    def as_thread(self, commander: str) -> ThreadCaller: ...

    def matches_thread(self, registry: Registration, name: str) -> bool:
        return False

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return comms.registry.status(name).permits_reconfiguration(self, comms.registry, name)

    @abstractmethod
    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller whose sends direct units refuses a body its directive checks refuse."""

    def origin(self) -> AgentOrigin:
        """Who this caller's sends speak for; an agent speaks only for itself."""
        return AgentOrigin()


@dataclass(frozen=True)
class NoThreadCaller(ToolCaller):
    def acting_caller(self, grant: ToolCaller) -> ToolCaller:
        return grant

    def as_thread(self, commander: str) -> ThreadCaller:
        return ThreadCaller(commander)

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

    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller with no thread gives no directive."""


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def as_thread(self, commander: str) -> ThreadCaller:
        return self

    def commander(self, comms: Comms) -> Thread:
        return comms.registry.require(self.name)

    def identity(self, comms: Comms) -> ThreadIncarnation:
        return comms.registry.require(self.name).incarnation

    def head_review_actor(self, comms: Comms, request: PullRequestHead,
                          order: HeadReviewOrder) -> ThreadIncarnation:
        actor = self.identity(comms)
        order.require_reviewer(comms.goals, request, actor)
        return actor

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

    def matches_thread(self, registry: Registration, name: str) -> bool:
        return registry.canonical_name(name) == registry.canonical_name(self.name)

    def require_directive(self, comms: Comms, body: str) -> None:
        """A unit's or a script's send is a message, not a directive."""


@dataclass(frozen=True)
class CommanderForkCaller(ThreadCaller):
    """Commander verbs use a registered child without copying registry procedures."""

    def commanding(self, comms: Comms, commander: str) -> None:
        comms.registry.register_commander_child(self.name, commander, comms.goals)

    def commander(self, comms: Comms) -> Thread:
        fork = comms.registry.require(self.name)
        parent, = fork.parent.names()
        return comms.registry.require(parent)

    def identity(self, comms: Comms) -> ThreadIncarnation:
        fork = comms.registry.require(self.name)
        return fork.incarnation.require_recorded().acting_for(self.commander(comms).incarnation)

    def retire(self, comms: Comms) -> None:
        """A fork stops only after the bus holds something it sent, since every commander verb
        that records announces it there; its thread is then archived, and a resumed fork of
        the same name redeclares onto it."""
        if self.name not in comms.bus.bus.activity_clocks().sent:
            raise ToolRefusedError(
                f"{self.name} recorded nothing in Core: post what it found with"
                f" `agent-comms-commander order say '#comms' FILE --fork {self.name}` before it stops.")
        comms.registry.unregister(self.name)
        comms.threads.archive(self.name)
