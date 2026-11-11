"""Standing rules on what a directed unit says, read from its own published messages.

Waste has to flag itself: a unit that talks far more than it commits gets a named board row
without anyone reading #comms by hand. A stall is never read from prose: a phrase match over
"blocked on" flagged descriptions as stalls, so a stall row comes only from a measured fact
(a Claude fork's transcript gone quiet is ForkStalled in agent_comms.team.standing).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from itertools import takewhile
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.team.standing_rules import FamilyScan, HolderLine, UndatedStandingRule
from agent_comms.ui_model.teammate_state import TeammateObservation

if TYPE_CHECKING:
    from agent_comms.messaging.bus_publication import Delivered
    from agent_comms.team.standing_rules import HeartbeatFrame
    from agent_comms.ui_model.teammate_state import TeammateState


@dataclass(frozen=True)
class TeammateTalk:
    """One observed unit's own messages and commits over the rules' shared window."""

    source: TeammateObservation
    window: ClassVar[float] = 3600
    page: ClassVar[int] = 25
    pages: ClassVar[int] = 20

    @property
    def unit(self) -> ThreadIncarnation:
        return self.source.thread.incarnation

    @property
    def since(self) -> float:
        return self.source.now - self.window

    def publications(self) -> Iterator[Delivered]:
        """Its own publications, newest first, one page of the bus's sent index at a time."""
        bus = self.source.comms.bus.bus
        lookup = self.unit.require_recorded().lookup
        before = None
        for _ in range(self.pages):
            page = bus.sent(lookup, before=before, limit=self.page)
            for record in page:
                yield from record.deliveries()
            if len(page) < self.page:
                return
            before = page[-1].seq

    @cached_property
    def messages(self) -> tuple[Delivered, ...]:
        """The window's publications: a quiet unit decodes one page, never its whole history."""
        return tuple(takewhile(lambda message: message.timestamp >= self.since,
                               self.publications()))

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


class TeammateTalkRule(HolderLine, FamilyScan, declared_name=None):
    """Rules over one unit's talk share the heartbeat's existing unit observations."""

    @classmethod
    def scan_observed(
        cls, comms: Comms, observed: Sequence[TeammateState], frame: HeartbeatFrame
    ) -> tuple[Self, ...]:
        # An unreadable source has no readable unit, so its talk is never re-read here.
        talks = tuple(TeammateTalk(state.source)
                      for state in observed for _unit in state.readable_units())
        return tuple(
            rule
            for member in cls.members_with(cls)
            for talk in talks
            for rule in member.derive(talk)
        )

    @classmethod
    @abstractmethod
    def derive(cls, talk: TeammateTalk) -> Iterator[Self]: ...


@dataclass(frozen=True)
class Ceremony(TeammateTalkRule, UndatedStandingRule):
    """A unit publishes far more text than it commits: the report has become the work."""

    characters: int = field(compare=False)
    commits: int = field(compare=False)
    per_commit: ClassVar[int] = 6000

    @classmethod
    def derive(cls, talk: TeammateTalk) -> Iterator[Self]:
        # Only a unit holding pending work owes commits; an idle or review-only unit does not.
        if talk.source.pending and talk.characters > cls.per_commit * max(talk.commits, 1):
            yield cls(holder=talk.unit, characters=talk.characters, commits=talk.commits)

    def line(self) -> str:
        return (
            f"CEREMONY {self.holder.name} published {self.characters} characters and"
            f" {self.commits} commits in the last hour, limit {self.per_commit} per commit."
            " the receipt printer is louder than the compiler -> commit, diff stat,"
            " file:line, Assumed:, nothing else"
        )
