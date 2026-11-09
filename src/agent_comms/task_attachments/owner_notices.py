"""What Core says in the owner's voice, under his own thread.

The heartbeat, its escalations and a restarted unit's reminder are his: he set every rule they
enforce, and a reader who sees a service name skims them as automation. So they publish under
the human thread his typed messages come from. What separates them from a message he typed is
the notice task each one carries: a notice directs nothing, records no request and adds no owner
words to any task. The sender says who speaks; the task says nobody typed it.
"""
from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.errors import RelationViolationError
from agent_comms.messaging.channel_targets import CommandTarget, CommsTag, NamedTarget, ThreadTarget
from agent_comms.messaging.messages import (
    ConversationRole,
    EscalationPosition,
    Message,
    MessageRole,
    NoticeRole,
)
from agent_comms.messaging.projection_source import ProducedRow
from agent_comms.messaging.task_sources import TaskAttachment
from agent_comms.registry.thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.context_segments.wake import MessageDelivery
    from agent_comms.execution.turn_context import ContextSegment
    from agent_comms.messaging.bus import BusReader
    from agent_comms.messaging.sending import Messaging
    from agent_comms.registry.registry_document import RegistrySnapshot
    from agent_comms.storage.typed_table import TypedTable


@dataclass(frozen=True)
class OwnerNotice(TaskAttachment, declared_name=None):
    """A message Core writes for the owner and publishes under his thread."""

    role: ClassVar[MessageRole] = ConversationRole()

    def user_task_facts(self, message: Message) -> tuple[()]:
        """Nobody typed it, so it is no owner source for any task."""
        return ()

    def require_publication(self, sender: str, registry: RegistrySnapshot,
                            original_source: BusReader) -> None:
        registry.require(sender).role.require_user(
            RelationViolationError("Only the owner's own thread carries an owner notice"))

    def publish(self, messaging: Messaging, target: NamedTarget, body: str) -> Message:
        return messaging.send_owner_notice(target, body, self, role=self.role)


@dataclass(frozen=True)
class RestartNotice(OwnerNotice, declared_name="restart_notice"):
    """A unit restarted while holding orders is told to continue them."""


@dataclass(frozen=True)
class OrderNotice(OwnerNotice, declared_name="order_notice"):
    """Core's own word to a holder about its order (an unowned order given to it, its branch
    behind main): Core acts, so no teammate is made its sender."""


@dataclass(frozen=True)
class MeantNotice(OwnerNotice, declared_name="meant_question"):
    """The rule gate asks a session whether it meant the answers it just wrote, and tells off a
    dodge (rule_deal.MeantRecord). It lands mid-turn like any line of his."""

    def message_segment(self, delivery: MessageDelivery) -> ContextSegment:
        """The owner's question is a user contribution on either input route."""
        return delivery.typed()


@dataclass(frozen=True)
class TeamNotice(OwnerNotice, declared_name="fleet_notice"):
    """A line for the whole fleet's channel; it informs and starts no member's turn."""

    role = NoticeRole()

    def announce(self, messaging: Messaging, body: str) -> Message:
        return self.publish(messaging, CommandTarget.named(CommsTag.channel()), body)


@dataclass(frozen=True)
class EscalationNotice(OwnerNotice, declared_name=None):
    """One line of a burst to a teammate birth. Escalations arrive as a burst of short messages
    a beat apart, so each lands as its own input: one wall of text is skimmed, a burst is not.
    An answer naming any line covers every line of that burst sent before it."""

    teammate: ThreadIncarnation
    gap: ClassVar[float] = 2.0
    horizon: ClassVar[int] = 6 * 60 * 60
    title: ClassVar[str]

    writes: ClassVar[tuple[type[ProducedRow], ...]] = (EscalationPosition,)

    def projections(self, message: Message) -> Iterator[TypedTable]:
        yield EscalationPosition(message.seq, self.teammate.require_recorded().lookup, message.timestamp)

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        if registry.require(target).incarnation != self.teammate:
            raise RelationViolationError("An escalation changed recipient incarnation")

    @classmethod
    def of(cls, snapshot: RegistrySnapshot, teammate: str) -> EscalationNotice:
        return cls(snapshot.require(teammate).incarnation)

    @classmethod
    def burst(cls, comms: Comms, teammate: ThreadIncarnation, lines: Sequence[str]) -> None:
        """Every line is addressed to this birth; require_target refuses a replacement."""
        notice = cls(teammate)
        first, *rest = lines
        notice.publish(comms.messaging, ThreadTarget(teammate.name), first)
        for line in rest:
            time.sleep(cls.gap)
            notice.publish(comms.messaging, ThreadTarget(teammate.name), line)

    @classmethod
    def burst_once(cls, comms: Comms, teammate: ThreadIncarnation, lines: Sequence[str],
                   identity: str) -> None:
        """A burst is sent once per identity line: the bus, not a process's memory, says it
        went out, so a restarted heartbeat that sees the rule as new again does not resend it."""
        with comms.bus.bus.reading() as source:
            sent = cls.lines(source, comms.registry.snapshot(), teammate.name, time.time())
        if not any(message.body == identity for message in sent):
            cls.burst(comms, teammate, lines)

    @classmethod
    def lines(cls, source: BusReader, snapshot: RegistrySnapshot, teammate: str,
              before: float) -> tuple[Message, ...]:
        """This burst kind's lines to the teammate's birth within the horizon, newest first."""
        notice = cls.of(snapshot, teammate)
        with source.observing() as db:
            sent = EscalationPosition.sent(
                db, notice.teammate.require_recorded().lookup, time.time() - cls.horizon, before)
        return tuple(message for message in sent if message.task == notice)

    @classmethod
    def newest_answer(cls, source: BusReader, snapshot: RegistrySnapshot, teammate: str,
                      lines: Sequence[Message]) -> tuple[Message, ...]:
        """The newest answer naming any of those lines from the addressed teammate itself. The
        lines are already this birth's (lines() reads by its lookup); another teammate's
        reply, quote included, never covers or releases them."""
        return source.latest_reply(
            tuple(line.reference for line in lines), frozenset(snapshot.names_for(teammate)))

    @classmethod
    def unanswered(cls, source: BusReader, snapshot: RegistrySnapshot, teammate: str,
                   before: float) -> tuple[Message, ...]:
        """Lines sent by ``before`` that the bus ordered after the newest answer to any line of
        this burst kind. Whether a line is answered is decided by bus sequence alone, the one total
        order every writer shares: a reply whose clock ran behind still covers what it follows."""
        sent = cls.lines(source, snapshot, teammate, time.time())
        covered = max((answer.seq for answer in cls.newest_answer(source, snapshot, teammate, sent)),
                      default=0)
        return tuple(message for message in sent if message.seq > covered and message.timestamp <= before)


@dataclass(frozen=True)
class VibeCheckNotice(EscalationNotice, declared_name="vibe_check"):
    title = "vibe check"


@dataclass(frozen=True)
class ChokeholdNotice(EscalationNotice, declared_name="chokehold"):
    title = "chokehold"
