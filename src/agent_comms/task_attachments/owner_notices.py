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

from agent_comms.channel_targets import CommandTarget, CommsTag, NamedTarget, ThreadTarget
from agent_comms.errors import RelationViolationError
from agent_comms.messages import ConversationRole, EscalationPosition, Message, MessageRole, NoticeRole
from agent_comms.projection_source import ProducedRow
from agent_comms.task_sources import TaskAttachment
from agent_comms.thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from agent_comms.bus import BusReader
    from agent_comms.comms import Comms
    from agent_comms.messaging import Messaging
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.typed_table import TypedTable


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
class FleetNotice(OwnerNotice, declared_name="fleet_notice"):
    """A line for the whole fleet's channel; it informs and starts no member's turn."""

    role = NoticeRole()

    def announce(self, messaging: Messaging, body: str) -> Message:
        return self.publish(messaging, CommandTarget.named(CommsTag.channel()), body)


@dataclass(frozen=True)
class EscalationNotice(OwnerNotice, declared_name=None):
    """One line of a burst to a commander birth. Escalations arrive as a burst of short messages
    a beat apart, so each lands as its own input: one wall of text is skimmed, a burst is not.
    An answer naming any line covers every line of that burst sent before it."""

    commander: ThreadIncarnation
    gap: ClassVar[float] = 2.0
    horizon: ClassVar[int] = 6 * 60 * 60
    title: ClassVar[str]

    writes: ClassVar[tuple[type[ProducedRow], ...]] = (EscalationPosition,)

    def projections(self, message: Message) -> Iterator[TypedTable]:
        yield EscalationPosition(message.seq, self.commander.require_recorded().lookup, message.timestamp)

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        if registry.require(target).incarnation != self.commander:
            raise RelationViolationError("An escalation changed recipient incarnation")

    @classmethod
    def of(cls, snapshot: RegistrySnapshot, commander: str) -> EscalationNotice:
        return cls(snapshot.require(commander).incarnation)

    @classmethod
    def burst(cls, comms: Comms, commander: str, lines: Sequence[str]) -> None:
        notice = cls.of(comms.registry.snapshot(), commander)
        first, *rest = lines
        notice.publish(comms.messaging, ThreadTarget(commander), first)
        for line in rest:
            time.sleep(cls.gap)
            notice.publish(comms.messaging, ThreadTarget(commander), line)

    @classmethod
    def burst_once(cls, comms: Comms, commander: str, lines: Sequence[str], identity: str) -> None:
        """A burst is sent once per identity line: the bus, not a process's memory, says it
        went out, so a restarted heartbeat that sees the rule as new again does not resend it."""
        with comms.bus.bus.reading() as source:
            sent = cls.lines(source, comms.registry.snapshot(), commander, time.time())
        if not any(message.body == identity for message in sent):
            cls.burst(comms, commander, lines)

    @classmethod
    def lines(cls, source: BusReader, snapshot: RegistrySnapshot, commander: str,
              before: float) -> tuple[Message, ...]:
        """This burst kind's lines to the commander's birth within the horizon, newest first."""
        notice = cls.of(snapshot, commander)
        with source.observing() as db:
            sent = EscalationPosition.sent(
                db, notice.commander.require_recorded().lookup, time.time() - cls.horizon, before)
        return tuple(message for message in sent if message.task == notice)

    @classmethod
    def newest_answer(cls, source: BusReader, snapshot: RegistrySnapshot, commander: str,
                      lines: Sequence[Message]) -> tuple[Message, ...]:
        """The newest answer naming any of those lines from this commander or any commander of
        the bus: every one of them holds the gate this burst closes, so one answer opens it."""
        answerers = (commander, *(thread.name for thread
                                  in snapshot.commander_project.commanders(snapshot)))
        return source.latest_reply(
            tuple(line.reference for line in lines),
            frozenset().union(*(snapshot.names_for(name) for name in answerers)))

    @classmethod
    def unanswered(cls, source: BusReader, snapshot: RegistrySnapshot, commander: str,
                   before: float) -> tuple[Message, ...]:
        """Lines sent by ``before`` that the bus ordered after the newest answer to any line of
        this burst kind. Whether a line is answered is decided by bus sequence alone, the one total
        order every writer shares: a reply whose clock ran behind still covers what it follows."""
        sent = cls.lines(source, snapshot, commander, time.time())
        covered = max((answer.seq for answer in cls.newest_answer(source, snapshot, commander, sent)),
                      default=0)
        return tuple(message for message in sent if message.seq > covered and message.timestamp <= before)


@dataclass(frozen=True)
class VibeCheckNotice(EscalationNotice, declared_name="vibe_check"):
    title = "vibe check"


@dataclass(frozen=True)
class ChokeholdNotice(EscalationNotice, declared_name="chokehold"):
    title = "chokehold"
