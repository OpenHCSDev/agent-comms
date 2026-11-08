from abc import ABC


class TurnOutcome(ABC):
    """A completed Core turn task, independent of its transport's response."""

    __slots__ = ()
