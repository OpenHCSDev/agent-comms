"""Routing: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Annotated

from agent_comms.errors import RelationViolationError
from agent_comms.execution.input_key import InputKey
from agent_comms.execution.turn_origin import TurnOrigin
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.messaging.read_basis import DMDisplayScope, MessageDisplayScope
from agent_comms.messaging.response_policy import NO_ALIASES
from agent_comms.nominal.field_codec import (
    FieldCodec,
    NullMember,
    PresentMember,
    RecordMember,
    SubclassUnion,
    ThreadLookup,
    WireOmitDefault,
)
from agent_comms.planning.goal_scope import AllGoals, GoalScope
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogDocument
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.tagged_thread_source import TaggedThreadSource
from agent_comms.registry.thread_creation import ThreadCreationSource


class NoTurnOrigin(NullMember, TurnOrigin[Message]):
    """An autonomous turn has no message, delivery key or reply destination."""

    separate_turn = False
    autonomous = True

    def messages(self) -> tuple[Message, ...]:
        return ()

    @property
    def batch_target(self) -> NoTurnOrigin:
        return self

    def bus_keys(self, owner: ThreadCreationSource) -> tuple[InputKey, ...]:
        return ()


NO_TURN_ORIGIN = NoTurnOrigin()


@dataclass(frozen=True, slots=True)
class MessageRoute(PresentMember):
    sender: str
    targets: tuple[str, ...]

    @property
    def incoming_scope(self) -> str:
        return (
            ", ".join(
                CommandTarget.named(target).incoming_scope for target in self.targets
            )
            or "direct message"
        )

    @classmethod
    def from_wire(cls, data: Mapping) -> MessageRoute:
        return FieldCodec.decode(cls, data)

    def require_publications(self, originals: tuple[Delivered, ...], owner_lookup: ThreadLookup) -> None:
        if len(originals) > len(self.targets):
            raise RelationViolationError("Publication exceeds this turn's reply intent")
        for original in originals:
            if original.sender_lookup != owner_lookup:
                raise RelationViolationError("Publication belongs to another original sender")
            if original.message.target not in self.targets:
                raise RelationViolationError("Publication is outside this turn's reply targets")


@dataclass(frozen=True, slots=True)
class NoMessageRoute(NullMember):
    """The turn has no outgoing reply intent."""

    def require_publications(self, originals: tuple[Delivered, ...], owner_lookup: ThreadLookup) -> None:
        if originals:
            raise RelationViolationError("Publication exceeds this turn's reply intent")


class Routing(SubclassUnion, affix="Routing"):
    """A recorded route or its declared absence; readers ask the same owner."""

    @property
    def requests(self) -> tuple[MessageReference, ...]:
        return ()

    @property
    def publications(self) -> tuple[MessageReference, ...]:
        return ()

    @property
    def incoming(self) -> bool:
        return False

    @property
    def outgoing(self) -> bool:
        return False

    def validate_input(self, sent_text: PresentMember | NullMember) -> None:
        pass

    def with_requests(self, requests: tuple[MessageReference, ...]) -> Routing:
        return self


@dataclass(frozen=True, slots=True)
class NoTurnRouting(NullMember, Routing):
    """No route was recorded for this native item."""


@dataclass(frozen=True, slots=True)
class TurnRouting(RecordMember, Routing):
    requests: tuple[MessageReference, ...] = ()
    reply: MessageRoute | NoMessageRoute = NoMessageRoute()
    publications: Annotated[
        tuple[MessageReference, ...], WireOmitDefault()
    ] = ()

    @property
    def incoming(self) -> bool:
        return bool(self.requests)

    @property
    def outgoing(self) -> bool:
        return bool(self.reply.present())

    def validate_input(self, sent_text: PresentMember | NullMember) -> None:
        sent_text.require_present()
        self.reply.require_absent("Input routing requires exact sent text and no outgoing reply claim.")

    def with_requests(self, requests: tuple[MessageReference, ...]) -> TurnRouting:
        return replace(self, requests=requests, publications=())

    def requires_annotation(self, published: tuple[MessageReference, ...]) -> bool:
        return bool(self.requests or published)

    def annotation_ids(self, reader, checkpoint, runtime, through: int):
        return (reader.annotation_ids(checkpoint.offset, through)
                if self.requests and runtime.read_key == checkpoint.session_file else ())

    def require_publications(self, originals: tuple[Delivered, ...], owner_lookup: ThreadLookup) -> None:
        """Only committed originals addressed by this reply intent can be joined."""
        if self.publications:
            raise RelationViolationError("Turn intent cannot preclaim a publication")
        self.reply.require_publications(originals, owner_lookup)


@dataclass(frozen=True, slots=True)
class ScheduledTurn:
    prompt: str
    origin: TurnOrigin[Message] = NO_TURN_ORIGIN
    goal_id: GoalScope = AllGoals()

    @property
    def autonomous_goal(self) -> bool:
        return self.goal_id.schedules_goal() and self.origin.autonomous

    @classmethod
    def resuming_in_project(cls, project: Checkout) -> ScheduledTurn:
        return cls(
            f"Project change completed: tools and context now use {str(project.path)!r}. "
            "Continue the user's previous request from this directory. "
            "If the request was only to switch projects, report that you are ready; "
            "do not invent extra work."
        )

    @classmethod
    def incoming(
        cls, message: Message, *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> ScheduledTurn:
        guidance = message.role.response_policy(message).guidance(message, aliases=aliases)
        scope = CommandTarget.named(message.target).guidance_scope
        return cls(
            f"[agent-comms from {message.sender} to {message.target}]\n"
            f"[Response policy: {guidance}{scope}]\n{message.body}",
            origin=message,
        )

    @staticmethod
    def take_batch(pending: list[ScheduledTurn]) -> tuple[list[ScheduledTurn], list[ScheduledTurn]]:
        """Combine channel turns sharing a route, keeping each direct input separate."""
        if not pending:
            return [], []
        if pending[0].origin.separate_turn:
            return pending[:1], pending[1:]
        boundary = next(
            (
                index
                for index, turn in enumerate(pending)
                if (turn.origin.batch_target != pending[0].origin.batch_target
                    or turn.origin.separate_turn)
            ),
            len(pending),
        )
        return pending[:boundary], pending[boundary:]


@dataclass(frozen=True, slots=True)
class DeliveryScope(MessageDisplayScope):
    actor: str
    aliases: Mapping[str, str]
    channels: frozenset[str]

    @classmethod
    def capture(
        cls, name: str, snapshot: RegistryNames[TaggedThreadSource], catalog: CatalogDocument
    ) -> DeliveryScope:
        thread = snapshot.require(name)
        return cls(thread.name, snapshot.aliases, catalog.targets_for(thread.tags))

    @property
    def index_targets(self) -> frozenset[str]:
        return self.channels | frozenset(
            name for name in (self.actor, *self.aliases) if self.canonical(name) == self.actor
        )

    @property
    def condition(self) -> tuple[str, tuple]:
        targets = tuple(self.index_targets)
        return "{target} IN (" + ",".join("?" for _ in targets) + ")", targets

    def includes(self, message: Message) -> bool:
        return self.delivers(message.sender, message.target)

    def selection(self, target: CommandTarget, snapshot, catalog) -> MessageDisplayScope:
        return target.display_scope(self.actor, snapshot, catalog, DMDisplayScope)

    def canonical(self, name: str) -> str:
        return self.aliases.get(name, name)

    def delivers(self, sender: str, target: str) -> bool:
        return self.canonical(sender) != self.actor and (
            target in self.channels or self.canonical(target) == self.actor
        )

    @staticmethod
    def delivers_sql(actor: str, sender: str, canonical_sender: str) -> str:
        """Sender exclusion for SQL routes already selected by index_targets."""
        return f"coalesce({canonical_sender},{sender})<>{actor}"

    def conversation(self, sender: str, target: str) -> str:
        return CommandTarget.named(target).conversation(sender, self.actor, self.canonical)
