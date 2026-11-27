"""Callback memberships consumed by original task attachments."""

from abc import ABC, abstractmethod


class DeliveredContext[Segment](ABC):
    __slots__ = ()

    @abstractmethod
    def delivered(self) -> Segment: ...


class TeamObservation(ABC):
    """Only declared retained teammate observations belong to this result set."""
    __slots__ = ()
