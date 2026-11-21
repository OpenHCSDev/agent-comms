"""The sizes a conversation's recorded context had around one turn.

A context language model compacts inside its own turn, so the only place the protocol can see
the change is the context history that records what the harness sent: the size before the turn
and the size after it. Both are stated by the harness that holds the context, so this leaf
record carries them from the host that recorded them to the events that observe them; nothing
here derives a size of its own.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextSizes:
    """A conversation's context size at its previous record and at the one this turn made.

    Both are ``None`` when nothing recorded a size: a conversation that has never published a
    view, or one whose views predate the harness stating its estimate.
    """

    before: int | None
    after: int | None

    @classmethod
    def unrecorded(cls) -> ContextSizes:
        """What a host that keeps no context history has to say about a turn."""
        return cls(None, None)

    @property
    def shrank(self) -> bool:
        """Whether this turn replaced part of its context with less than it had before."""
        return None not in (self.before, self.after) and self.after < self.before
