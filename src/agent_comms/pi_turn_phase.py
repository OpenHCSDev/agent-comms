"""Pi event declarations own their transitions into shared turn phases."""

from __future__ import annotations

from typing import ClassVar

from . import turn_phase as phases


class PiTurnPhase:
    """Pi declarations own event entry/end matching; shared phases own stage behavior."""

    _start_phases: ClassVar[dict[type[object], tuple[type[phases.TurnPhase], ...]]] = {}
    _end_events: ClassVar[dict[type[object], frozenset[type[object]]]] = {}

    def on(self, current: phases.TurnPhase) -> phases.TurnPhase:
        for event_type in type(self).__mro__:
            for member in self._start_phases.get(event_type, ()):
                return self.enter(member, current)
        return self.leave(current) if self.matches_end(current) else current

    def enter(self, member: type[phases.TurnPhase], current: phases.TurnPhase) -> phases.TurnPhase:
        return member()

    def matches_end(self, current: phases.TurnPhase) -> bool:
        return any(
            event_type in self._end_events.get(member, frozenset())
            for member in type(current).__mro__
            for event_type in type(self).__mro__
        )

    def leave(self, current: phases.TurnPhase) -> phases.TurnPhase:
        return phases.ModelWaitPhase()
