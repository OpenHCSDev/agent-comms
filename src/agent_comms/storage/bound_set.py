"""A set bound as SQL parameters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BoundSet[Member: (str, int)]:
    """A set bound as SQL ``IN`` parameters. A set has no order, but a parameter tuple does,
    and a frozenset iterates in an order that depends on how it was built: binding it sorted
    makes equal sets bind equal statements, so a query keyed by its parameters is the same
    query however its set was assembled."""

    members: frozenset[Member]

    def within(self, column: str) -> str:
        """The membership test of ``column`` in this set, one placeholder per member."""
        return f"{column} IN ({','.join('?' for _ in self.members)})"

    @property
    def parameters(self) -> tuple[Member, ...]:
        return tuple(sorted(self.members))
