"""Declaration-owned journal states and pending-intent reconciliation."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .goal_history import GoalHistoryEntry, GoalHistoryStore
    from .goals import Goal


class GoalHistoryState(DeclaredFamily):
    @classmethod
    def reconcile(cls, entry: GoalHistoryEntry, journal: GoalHistoryStore,
                  current: Goal | None) -> None:
        """Settled evidence is not reconsidered against later registry values."""


class Pending(GoalHistoryState):
    @classmethod
    def reconcile(cls, entry: GoalHistoryEntry, journal: GoalHistoryStore,
                  current: Goal | None) -> None:
        if current == entry.after:
            journal.sync_visible_registry()
            state = Committed
        elif current == entry.before:
            state = Aborted
        else:
            state = Uncertain
        journal.set_state(entry.sequence, state)


class Committed(GoalHistoryState):
    """The original transition is durably visible."""


class Aborted(GoalHistoryState):
    """The original preimage is still visible."""


class Uncertain(GoalHistoryState):
    """Neither original endpoint can be attested; no transition is invented."""
