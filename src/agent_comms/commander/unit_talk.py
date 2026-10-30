"""Standing rules on what a commanded unit says, read from its own published messages.

Waste has to flag itself: a unit that talks far more than it commits gets a named board row
without anyone reading #comms by hand. A stall is never read from prose: a phrase match over
"blocked on" flagged descriptions as stalls, so a stall row comes only from a measured fact
(a Claude fork's transcript gone quiet is ForkStalled in agent_comms.commander.standing).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.standing_rules import (
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
            message
            for record in self.source.comms.bus.bus.sent(
                self.unit.require_recorded().lookup, limit=500
            )
            for message in record.deliveries()
            if message.timestamp >= self.since
        )

    @cached_property
    def commits(self) -> int:
        """Distinct commits its held orders' sources recorded in the window."""
        return len(
            {
                point
                for origin in self.source.sources
                for point in origin.commits()
                if point.at >= self.since
            }
        )

    @property
    def characters(self) -> int:
        return sum(message.text_size for message in self.messages)


class UnitTalkRule(HolderLine, FamilyScan, declared_name=None):
    """Rules over one unit's talk share the heartbeat's existing unit observations."""

    @classmethod
    def scan_observed(
        cls, comms: Comms, commander: str, observed: Sequence[UnitState], frame: HeartbeatFrame
    ) -> tuple[Self, ...]:
        talks = tuple(UnitTalk(state.source) for state in observed)
        return tuple(
            rule
            for member in cls.members_with(cls)
            for talk in talks
            for rule in member.derive(talk)
        )

    @classmethod
    @abstractmethod
    def derive(cls, talk: UnitTalk) -> Iterator[Self]: ...


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
        return (
            f"CEREMONY {self.holder.name} published {self.characters} characters and"
            f" {self.commits} commits in the last hour, limit {self.per_commit} per commit."
            " the receipt printer is louder than the compiler -> commit, diff stat,"
            " file:line, Assumed:, nothing else"
        )
