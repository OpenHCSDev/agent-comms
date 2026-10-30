"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from agent_comms_hooks.watch import WatchClaim

from .bus_publication import AgentOrigin
from .channel_targets import CommandersTag
from .cli_result import ThreadToolCatalogResult, ToolCatalogResult
from .errors import ToolRefusedError
from .field_codec import WireForm
from .launch_root import LaunchRoot
from .read_basis import OperatorOutcomes, ReadAuthority
from .notification_evidence import OutcomeViewer, OwnerOutcomes
from .presentation import SentMessageSummary, ThreadRecord
from .thread_execution import CommanderForkExecution
from .thread_identity import ThreadIncarnation
from .tool_result import ToolResult
from .turn_phase import IdlePhase

if TYPE_CHECKING:
    from .comms import Comms
    from .head_review import HeadReviewOrder, PullRequestHead
    from .threads import Thread
    from .tool_request import ToolRequest


@dataclass(frozen=True)
class ThreadListing(ToolResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]
    sent_messages: tuple[SentMessageSummary, ...]


class ToolCaller(ReadAuthority):
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

    def commanding(self, comms: Comms) -> None:
        """An ordinary caller needs no commander-child registration."""
        return None

    def join(self, comms: Comms, parent: str) -> None:
        """An ordinary caller is nobody's child."""
        return None

    def retire(self, comms: Comms) -> None:
        self.end(comms)

    def end(self, comms: Comms) -> None:
        raise ToolRefusedError('Only a commander fork can retire through this verb.')

    @abstractmethod
    def as_thread(self, commander: str) -> ThreadCaller: ...

    @abstractmethod
    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller whose sends direct units refuses a body its directive checks refuse."""

    def origin(self) -> AgentOrigin:
        """Who this caller's sends speak for; an agent speaks only for itself."""
        return AgentOrigin()


@dataclass(frozen=True)
class NoThreadCaller(OperatorOutcomes, ToolCaller):
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

    def acting_name(self, default: str) -> str:
        return default

    def from_flag(self) -> tuple[str, ...]:
        return ()

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
        return comms.registry.require(self.name).acting_identity

    def head_review_actor(self, comms: Comms, request: PullRequestHead,
                          order: HeadReviewOrder) -> ThreadIncarnation:
        actor = self.identity(comms)
        order.require_reviewer(comms.goals, request, actor)
        return actor

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> ToolResult:
        return request.apply(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        presentation = comms.views.thread_presentations((self.name,)).require(self.name)
        return ThreadListing(threads, SentMessageSummary.for_sources(presentation.sent_notifications()))

    def outcome_viewer(self, registry: Registration) -> OutcomeViewer:
        return OwnerOutcomes.of(registry.require(self.name).incarnation)

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

    def require_directive(self, comms: Comms, body: str) -> None:
        """A unit's or a script's send is a message, not a directive."""


@dataclass(frozen=True)
class SubagentCaller(ThreadCaller):
    """A child working inside its parent session's turn."""

    def end(self, comms: Comms) -> None:
        """A child that has stopped is archived, so no ended child stays subscribed; the next
        declaration of the same name redeclares onto it and keeps its history, and a child
        already archived has already ended."""
        if comms.registry.status(self.name).archived:
            return
        comms.registry.require(self.name).execution.require_subagent()
        self.leaving(comms)
        comms.agents.enter_phase(self.name, IdlePhase())
        comms.registry.unregister(self.name)
        comms.threads.archive(self.name)

    def leaving(self, comms: Comms) -> None:
        """What a child reports as it ends; a role slot's history is already on its thread."""


class ActsForCommander(ThreadCaller):
    """Recorded under its own incarnation, holding its commander's authority: the commander
    verbs accept its orders and verdicts as the commander's, and the record names who acted."""

    @abstractmethod
    def commander(self, comms: Comms) -> Thread: ...

    def identity(self, comms: Comms) -> ThreadIncarnation:
        own = comms.registry.require(self.name).incarnation
        return own.require_recorded().acting_for(self.commander(comms).acting_identity)


@dataclass(frozen=True)
class CommanderPeerCaller(ThreadCaller):
    """Another commander acting under its own name: membership in the commanders channel is
    the declaration, so its verdicts and orders record its own incarnation, never the
    registered commander's."""

    def commanding(self, comms: Comms) -> None:
        if CommandersTag.declared_name not in comms.registry.require(self.name).tags:
            raise ToolRefusedError(
                f"{self.name} is not in {CommandersTag.channel()}; join it to act as a commander.")

    def as_thread(self, commander: str) -> ThreadCaller:
        return CommanderPeerAuthority(self.name, commander)


@dataclass(frozen=True)
class CommanderPeerAuthority(ActsForCommander, CommanderPeerCaller):
    """A peer bound to the registered commander whose orders it judges and directs."""

    authority: str

    def as_thread(self, commander: str) -> ThreadCaller:
        return self

    def commander(self, comms: Comms) -> Thread:
        return comms.registry.require(self.authority)

    def identity(self, comms: Comms) -> ThreadIncarnation:
        own = comms.registry.require(self.name).incarnation.require_recorded()
        return own.peer_for(self.commander(comms).incarnation)


@dataclass(frozen=True)
class CommanderForkCaller(ActsForCommander, SubagentCaller):
    """Commander verbs use a registered child without copying registry procedures."""

    def commanding(self, comms: Comms) -> None:
        """A verb resumes the fork under the parent that spawned it, never under whichever
        thread the bus records as commander."""
        self.join(comms, self.spawner(comms))

    def join(self, comms: Comms, parent: str) -> None:
        """The SubagentStart hook declares the fork under the session that spawned it, in its
        own process, before the fork's first call; the child inherits that parent's tags.
        A connector grant names its holder, so a fork of another parent is refused."""
        comms.registry.register_child(self.name, parent, CommanderForkExecution, comms.goals)

    def spawner(self, comms: Comms) -> str:
        """The parent the hook registered; before it has, the bus thread the calling Claude
        session is bound to, which is the session that spawned this fork."""
        if self.name in comms.registry:
            parent, = comms.registry.require(self.name).parent.names()
            return parent
        claim = WatchClaim(WatchClaim.environment_session())
        bindings = claim.state().bindings()
        if not bindings:
            raise ToolRefusedError(
                f"{self.name} has no registered parent and Claude session {claim.thread}"
                " has no bus binding to name one.")
        binding, = bindings
        return binding.bus

    def commander(self, comms: Comms) -> Thread:
        fork = comms.registry.require(self.name)
        parent, = fork.parent.names()
        return comms.registry.require(parent)

    def recorded(self, comms: Comms) -> bool:
        """A fork's record in Core is what it sent on the bus or a head verdict it recorded:
        a verdict on an orderless head posts nothing, yet Core holds it."""
        own = comms.registry.require(self.name).incarnation.require_recorded()
        return (self.name in comms.bus.bus.activity_clocks().sent
                or comms.goals.todos.recorded_head_review(own))

    def retire(self, comms: Comms) -> None:
        """A fork asking to stop is sent back once to record what it found."""
        if not self.recorded(comms):
            raise ToolRefusedError(
                f"{self.name} recorded nothing in Core: record a head verdict with"
                f" `agent-comms-commander judge ... --fork {self.name}` or post what it found with"
                f" `agent-comms-commander order say '#comms' FILE --fork {self.name}` before it stops.")
        self.end(comms)

    def leaving(self, comms: Comms) -> None:
        """A fork that recorded nothing tells its commander the work is missing."""
        if not self.recorded(comms):
            commander = self.commander(comms).name
            comms.messaging.send(self.name, commander, (
                f"{self.name} stopped without recording what it found: its work is missing."
                " Read its transcript or give the work to another fork."))
