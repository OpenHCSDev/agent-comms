"""The caller bound at tool entry owns caller-relative tool behavior."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from agent_comms.cli_verbs.cli_result import ThreadToolCatalogResult, ToolCatalogResult
from agent_comms.coordination.tables.board_delivery import ReaderBoardDelivery
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.turn_phase import IdlePhase
from agent_comms.messaging.bus_publication import AgentOrigin
from agent_comms.messaging.notification_evidence import OutcomeViewer, OwnerOutcomes
from agent_comms.messaging.read_basis import DisplayViewer, OperatorOutcomes, ReadAuthority
from agent_comms.nominal.field_codec import FieldCodec, WireForm
from agent_comms.presentation import SentMessageSummary, ThreadRecord
from agent_comms.registry.registration import Registration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import ForkThread, SubagentThread, Thread
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError
from agent_comms_hooks.watch import SessionEndpoint

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.projects.head_review import HeadReviewOrder, PullRequestHead
    from agent_comms.transport.tool_request import ServedRequest


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
    def invoke(self, request: ServedRequest, comms: Comms) -> ToolResult: ...

    def board(self, comms: Comms) -> tuple[str, ...]:
        """The board rows a verb's reply carries for this caller; a caller acting as no thread
        is named by no row."""
        return ()

    @abstractmethod
    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing: ...

    @abstractmethod
    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult: ...

    @abstractmethod

    @abstractmethod
    def from_flag(self) -> tuple[str, ...]:
        """The command line's --from argument naming this caller, if it names a thread."""

    def rejoin(self, comms: Comms) -> None:
        """An ordinary caller needs no teammate-child registration."""
        return None

    def join(self, comms: Comms, parent: str) -> None:
        """An ordinary caller is nobody's child."""
        return None

    def retire(self, comms: Comms) -> None:
        self.end(comms)

    def end(self, comms: Comms) -> None:
        raise ToolRefusedError('Only a teammate fork can retire through this verb.')

    @abstractmethod
    def teammate(self) -> ThreadCaller:
        """The teammate a team verb acts and is recorded as; there is no lead to fall back on."""

    @abstractmethod
    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller whose sends direct units refuses a body its directive checks refuse."""

    def require_human(self) -> None:
        """Only a caller acting for the owner may publish human input; a sender name never can."""
        raise ToolRefusedError("Only a human UI caller can publish user messages.")

    def origin(self) -> AgentOrigin:
        """Who this caller's sends speak for; an agent speaks only for itself."""
        return AgentOrigin()


@dataclass(frozen=True)
class NoThreadCaller(OperatorOutcomes, ToolCaller):
    def history_viewer(self, named: str) -> DisplayViewer:
        """Omitted participant means an anonymous channel read, not an agent identity."""
        return FieldCodec.decode(DisplayViewer, named or None)

    def acting_caller(self, grant: ToolCaller) -> ToolCaller:
        return grant

    def teammate(self) -> ThreadCaller:
        raise ToolRefusedError(
            "Team verbs act as the teammate running them: pass --as NAME, or --fork NAME for a "
            "fork acting for its teammate. There is no lead to act as.")

    def invoke(self, request: ServedRequest, comms: Comms) -> ToolResult:
        return request.apply_without_thread(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        return ThreadListing(threads, ())

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult:
        return ToolCatalogResult(tools)


    def from_flag(self) -> tuple[str, ...]:
        return ()

    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller with no thread gives no directive."""


@dataclass(frozen=True)
class HumanCaller(NoThreadCaller):
    """An owner-operated UI has human authority without an executable thread."""

    def require_human(self) -> None:
        """The owner's UI may publish human input."""


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def teammate(self) -> ThreadCaller:
        return self

    def authority(self, comms: Comms) -> Thread:
        return comms.registry.require(self.name)

    def identity(self, comms: Comms) -> ThreadIncarnation:
        return comms.registry.require(self.name).acting_identity

    def head_review_actor(self, comms: Comms, request: PullRequestHead,
                          order: HeadReviewOrder) -> ThreadIncarnation:
        actor = self.identity(comms)
        order.require_reviewer(comms.goals, request, actor)
        return actor

    def invoke(self, request: ServedRequest[ThreadCaller], comms: Comms) -> ToolResult:
        return request.apply(comms, self)

    def board(self, comms: Comms) -> tuple[str, ...]:
        """The new rows naming this thread, stamped delivered in the call that returns them, so
        the session's hook and its verb replies print a row once between them."""
        return ReaderBoardDelivery.lines(comms, self.authority(comms).incarnation, time.time())

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
        comms.registry.require_member(self.name, SubagentThread)
        self.leaving(comms)
        comms.agents.enter_phase(self.name, IdlePhase())
        comms.registry.unregister(self.name)
        comms.threads.archive(self.name)

    def leaving(self, comms: Comms) -> None:
        """What a child reports as it ends; a role slot's history is already on its thread."""


class ActsForTeam(ThreadCaller):
    """Recorded under its own incarnation, holding its teammate's authority: the teammate
    verbs accept its orders and verdicts as the teammate's, and the record names who acted."""

    @abstractmethod
    def authority(self, comms: Comms) -> Thread: ...

    def identity(self, comms: Comms) -> ThreadIncarnation:
        own = comms.registry.require(self.name).incarnation
        return own.require_recorded().acting_for(self.authority(comms).acting_identity)


@dataclass(frozen=True)
class PeerCaller(ThreadCaller):
    """A teammate acts and is recorded under its own incarnation, independently of channel
    tags. There is no lead whose records a peer selects: its orders name it as creator."""


class ForkRecordOwed(ToolRefusedError):
    """A fork asked to stop before recording what it found: its stop is sent back once.
    Any other refusal (an unregistered fork, a foreign parent) is a failure, never a send-back."""


@dataclass(frozen=True)
class ForkCaller(ActsForTeam, SubagentCaller):
    """Team verbs use a registered child without copying registry procedures."""

    def rejoin(self, comms: Comms) -> None:
        """A verb resumes the fork under the parent that spawned it, never under whichever
        thread the bus records as teammate."""
        self.join(comms, self.spawner(comms))

    def join(self, comms: Comms, parent: str) -> None:
        """The SubagentStart hook declares the fork under the session that spawned it, in its
        own process, before the fork's first call; the child inherits that parent's tags.
        A connector grant names its holder, so a fork of another parent is refused."""
        comms.registry.register_child(self.name, parent, ForkThread, comms.goals)

    def spawner(self, comms: Comms) -> str:
        """The parent the hook registered; before it has, the bus thread the calling Claude
        session is bound to, which is the session that spawned this fork."""
        if self.name in comms.registry:
            parent, = comms.registry.require(self.name).parent.names()
            return parent
        claim = SessionEndpoint(SessionEndpoint.environment_session())
        bindings = claim.bindings()
        if not bindings:
            raise ToolRefusedError(
                f"{self.name} has no registered parent and Claude session {claim.thread}"
                " has no bus binding to name one.")
        binding, = bindings
        return binding.bus

    def authority(self, comms: Comms) -> Thread:
        fork = comms.registry.require(self.name)
        parent, = fork.parent.names()
        return comms.registry.require(parent)

    @staticmethod
    def end_session(comms: Comms, callers: tuple[ForkCaller, ...]) -> tuple[ForkCaller, ...]:
        """Only children that joined this bus can be ended; every entry uses the same owner."""
        registered = tuple(caller for caller in callers if caller.name in comms.registry)
        for caller in registered:
            caller.end(comms)
        return registered

    def recorded(self, comms: Comms) -> bool:
        """A fork's record in Core is what it sent on the bus or a head verdict it recorded:
        a verdict on an orderless head posts nothing, yet Core holds it."""
        own = comms.registry.require(self.name).incarnation.require_recorded()
        return (self.name in comms.bus.bus.activity_clocks().sent
                or comms.goals.todos.recorded_head_review(own))

    def retire(self, comms: Comms) -> None:
        """A fork asking to stop is sent back once to record what it found."""
        if not self.recorded(comms):
            raise ForkRecordOwed(
                f"{self.name} recorded nothing in Core: record a head verdict with"
                f" `agent-comms-team judge ... --fork {self.name}` or post what it found with"
                f" `agent-comms-team order say '#comms' FILE --fork {self.name}` before it stops.")
        self.end(comms)

    def leaving(self, comms: Comms) -> None:
        """A fork that recorded nothing tells its teammate the work is missing."""
        if not self.recorded(comms):
            teammate = self.authority(comms).name
            comms.messaging.send(self.name, teammate, (
                f"{self.name} stopped without recording what it found: its work is missing."
                " Read its transcript or give the work to another fork."))
