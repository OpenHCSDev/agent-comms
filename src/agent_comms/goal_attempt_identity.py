"""Goal attempt identity without the reservation token or execution authority."""

from dataclasses import dataclass


class FailureNotObserved(ValueError):
    """This lifecycle or record does not carry a bound passive failure."""


@dataclass(frozen=True, slots=True)
class GoalAttemptIdentity:
    goal_id: str
    generation: int
    attempt_id: str
