"""Routing: declaration and persistence owners."""

from __future__ import annotations

from .errors import RelationViolationError

from .input_key import InputKey

from . import turn_origin
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Annotated, TYPE_CHECKING


from .channel_targets import CommandTarget
from .field_codec import (
    ThreadLookup, FieldCodec, NullMember, RecordMember, SubclassUnion, WireOmitDefault, GoalId,
)
from .message_reference import MessageReference
from .messages import PublicSender
from .read_basis import DMDisplayScope, MessageDisplayScope

if TYPE_CHECKING:
    from .catalog_document import CatalogDocument
    from .registry_provenance import RegistryProvenance
    from .thread_identity import ThreadIncarnation
    from .input_disposition import InputDispositions
    from .messages import Message
    from .threads import Thread


class NoTurnOrigin(NullMember, turn_origin.TurnOrigin):
    """An autonomous turn has no message, delivery key or reply destination."""

    separate_turn = False
    autonomous = True

    def messages(self) -> tuple[Message, ...]:
        return ()

    @property
    def batch_target(self) -> NoTurnOrigin:
        return self

    def bus_keys(self, dispositions: InputDispositions, owner: Thread) -> tuple[InputKey, ...]:
        return ()


NO_TURN_ORIGIN = NoTurnOrigin()


@dataclass(frozen=True, slots=True)
class MessageRoute:
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

    def validate_input(self, sent_text: str | None) -> None:
        pass

    def with_requests(self, requests: tuple[MessageReference, ...]) -> Routing:
        return self


@dataclass(frozen=True, slots=True)
class NoTurnRouting(NullMember, Routing):
    """No route was recorded for this native item."""


@dataclass(frozen=True, slots=True)
class TurnRouting(RecordMember, Routing):
    requests: tuple[MessageReference, ...] = ()
    reply: MessageRoute | None = None
    publications: Annotated[
        tuple[MessageReference, ...], WireOmitDefault()
    ] = ()

    @property
    def incoming(self) -> bool:
        return bool(self.requests)

    @property
    def outgoing(self) -> bool:
        return self.reply is not None

    def validate_input(self, sent_text: str | None) -> None:
        if sent_text is None or self.outgoing:
            raise ValueError("Input routing requires exact sent text and no outgoing reply claim.")

    def with_requests(self, requests: tuple[MessageReference, ...]) -> TurnRouting:
        return replace(self, requests=requests, publications=())

    def requires_annotation(self, published: tuple[MessageReference, ...]) -> bool:
        return bool(self.requests or published)

    def annotation_ids(self, reader, checkpoint, runtime, through: int):
        return (reader.annotation_ids(checkpoint.offset, through)
                if self.requests and runtime.read_key == checkpoint.session_file else ())

    def require_publications(self, originals, owner_lookup: str) -> None:
        """Only committed originals addressed by this reply intent can be joined."""
        if self.publications:
            raise RelationViolationError("Turn intent cannot preclaim a publication")
        if not originals:
            return
        if self.reply is None or len(originals) > len(self.reply.targets):
            raise RelationViolationError("Publication exceeds this turn's reply intent")
        for original in originals:
            if original.sender_lookup != owner_lookup:
                raise RelationViolationError("Publication belongs to another original sender")
            if original.message.target not in self.reply.targets:
                raise RelationViolationError("Publication is outside this turn's reply targets")


@dataclass(frozen=True, slots=True)
class ScheduledTurn:
    prompt: str
    origin: turn_origin.TurnOrigin = NO_TURN_ORIGIN
    goal_id: GoalId | None = None
    goal_wait_id: str | None = None

    @property
    def autonomous_goal(self) -> bool:
        return self.goal_id is not None and self.origin.autonomous

    @classmethod
    def resuming_in_project(cls, project: str) -> ScheduledTurn:
        return cls(
            f"Project change completed: tools and context now use {project!r}. "
            "Continue the user's previous request from this directory. "
            "If the request was only to switch projects, report that you are ready; "
            "do not invent extra work."
        )

    def require_dependency_wake(self, error: Exception) -> None:
        """Only dependency launches use standby refusal settlement."""
        if self.goal_wait_id is None:
            raise error

    @classmethod
    def incoming(
        cls, message: Message, *, aliases: Mapping[str, str] | None = None
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
        cls, name: str, snapshot: RegistryProvenance, catalog: CatalogDocument
    ) -> DeliveryScope:
        thread = snapshot.require(name)
        return cls(thread.name, snapshot.aliases, catalog.targets_for(thread.tags))

    @property
    def index_targets(self) -> frozenset[str]:
        return self.channels | frozenset(
            name for name in (self.actor, *self.aliases) if self.canonical(name) == self.actor
        )

    @property
    def index_predicate(self) -> tuple[str, tuple]:
        targets = tuple(self.index_targets)
        return "target IN (" + ",".join("?" for _ in targets) + ")", targets

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
    def sender_lookups(
        source: ThreadIncarnation, on_unrecorded: Callable[[], None]
    ) -> Iterator[ThreadLookup | PublicSender]:
        """Share format discovery; each consumer owns its missing-birth action.

        The unstamped format matches before a birth is required. An index must
        probe for an assigned stamp; starting input requires a verified stamp.
        """
        def missing() -> tuple[ThreadLookup, ...]:
            on_unrecorded()
            return ()

        yield PublicSender()
        yield from source.recorded_or(lambda recorded: (recorded.lookup,), missing)

    def minimum_timestamp(
        self, sender: str, target: str, sender_lookup: ThreadLookup | PublicSender,
        snapshot: RegistryProvenance
    ) -> float | None:
        """Require current identities while preserving aliases of one incarnation."""

        if not self.delivers(sender, target):
            return None
        source = snapshot.threads.get(self.canonical(sender))
        if source is None:
            return None
        incarnation = source.incarnation
        if sender_lookup not in self.sender_lookups(incarnation, incarnation.require_recorded):
            return None
        return max(source.incarnation.wire_birth, snapshot.threads[self.actor].incarnation.wire_birth)

    def conversation(self, sender: str, target: str) -> str:
        return CommandTarget.named(target).conversation(sender, self.actor, self.canonical)
