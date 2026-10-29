"""Standing rules on what a commanded unit says, read from its own published messages.

Waste has to flag itself: a unit that announces a wait nobody owns, or talks far more than
it commits, gets a named board row without anyone reading #comms by hand. The rule text these
rows enforce is rules/no-blocking.md.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.field_codec import MessageId
from agent_comms.standing_rules import (
    DatedStandingRule,
    FamilyScan,
    HolderLine,
    UndatedStandingRule,
)
from agent_comms.thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from agent_comms.bus_publication import Delivered
    from agent_comms.heartbeat_frame import HeartbeatFrame
    from agent_comms.ui_model.unit_state import UnitObservation, UnitState


@dataclass(frozen=True)
class UnitTalk:
    """One observed unit's own messages and commits over the rules' shared window."""

    source: UnitObservation
    window: ClassVar[float] = 3600

    @property
    def unit(self) -> ThreadIncarnation:
        return self.source.thread.incarnation

    @property
    def since(self) -> float:
        return self.source.now - self.window

    @cached_property
    def messages(self) -> tuple[Delivered, ...]:
        """Its own publications in the window, newest first, through the bus's sent index."""
        return tuple(
            message for message in self.source.comms.bus.sent(
                self.unit.require_recorded().lookup, limit=500)
            if message.timestamp >= self.since
        )

    @cached_property
    def commits(self) -> int:
        """Distinct commits its held orders' sources recorded in the window."""
        return len({point for origin in self.source.sources for point in origin.commits()
                    if point.at >= self.since})

    @property
    def characters(self) -> int:
        return sum(message.text_size for message in self.messages)


class UnitTalkRule(HolderLine, FamilyScan, declared_name=None):
    """Rules over one unit's talk share the heartbeat's existing unit observations."""

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        talks = tuple(UnitTalk(state.source) for state in observed)
        return tuple(rule for member in cls.members_with(cls)
                     for talk in talks for rule in member.derive(talk))

    @classmethod
    @abstractmethod
    def derive(cls, talk: UnitTalk) -> Iterator[Self]: ...


@dataclass(frozen=True)
class BlockedTalk(UnitTalkRule, DatedStandingRule):
    """A unit says it is waiting, frozen or holding and names neither an owner nor a line.

    no-blocking.md: every wait names its owner and is announced; a wait nobody can act on is
    a deadlock nobody can see. A named thread or a file:line is what makes it actionable."""

    holder: ThreadIncarnation
    message_id: MessageId
    at: float = field(compare=False)
    quote: str = field(compare=False)
    words: ClassVar[tuple[str, ...]] = (
        "waiting on", "waiting for", "waits on", "awaiting", "blocked on", "blocked by",
        "blocked until", "frozen until", "frozen for", "head frozen", "holding until",
        "holding for", "on hold", "deferred to", "deferring to", "parked until",
        "until root", "pending root", "pending review",
    )
    """Phrases that announce a stop, not words that merely occur in one: "freeze is
    superseded" or "preserving behaviour" stop nothing."""
    stall: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<!not )(?<!n't )(?<!n’t )\b(" + "|".join(re.escape(word) for word in words) + r")\b",
        re.IGNORECASE)
    """A negated phrase ("not waiting on", "wasn't waiting for") announces the opposite."""
    located: ClassVar[re.Pattern[str]] = re.compile(r"[\w./-]+\.\w+:\d+")
    named_owner: ClassVar[re.Pattern[str]] = re.compile(r"@[\w-]+")

    @classmethod
    def derive(cls, talk: UnitTalk) -> Iterator[Self]:
        for message in talk.messages:
            said = cls.stall.search(message.body)
            if said and not (cls.located.search(message.body)
                             or cls.named_owner.search(message.body) or message.mentions):
                start = max(0, said.start() - 60)
                yield cls(talk.unit, message.message_id, message.timestamp,
                          " ".join(message.body[start:said.end() + 60].split()))

    def began(self) -> float:
        return self.at

    def line(self) -> str:
        return (f"BLOCKED-TALK {self.holder.name} said \"…{self.quote}…\" ({self.message_id}) with no"
                " owner and no file:line. waiting on whom, exactly? -> name the owner and the"
                " file:line, or do the next step now")


@dataclass(frozen=True)
class Ceremony(UnitTalkRule, UndatedStandingRule):
    """A unit publishes far more text than it commits: the report has become the work."""

    holder: ThreadIncarnation
    characters: int = field(compare=False)
    commits: int = field(compare=False)
    per_commit: ClassVar[int] = 6000

    @classmethod
    def derive(cls, talk: UnitTalk) -> Iterator[Self]:
        # Only a unit holding pending work owes commits; an idle or review-only unit does not.
        if talk.source.pending and talk.characters > cls.per_commit * max(talk.commits, 1):
            yield cls(talk.unit, talk.characters, talk.commits)

    def line(self) -> str:
        return (f"CEREMONY {self.holder.name} published {self.characters} characters and"
                f" {self.commits} commits in the last hour, limit {self.per_commit} per commit."
                " the receipt printer is louder than the compiler -> commit, diff stat,"
                " file:line, Assumed:, nothing else")
