"""Callback memberships consumed by original task attachments."""

from abc import ABC, abstractmethod

from agent_comms.execution.turn_context import ContextSegment


class DeliveredContext(ABC):
    __slots__ = ()

    @abstractmethod
    def delivered(self) -> ContextSegment: ...


class TeamObservation(ABC):
    """Only declared retained teammate observations belong to this result set."""
    __slots__ = ()
