"""Goal attempt identity without the reservation token or execution authority."""

from dataclasses import dataclass

from agent_comms.field_codec import GoalId


class FailureNotObserved(ValueError):
    """This lifecycle or record does not carry a bound passive failure."""

    def __init__(self, reason: str = "missing_binding") -> None:
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class GoalAttemptIdentity:
    goal_id: GoalId
    generation: int
    attempt_id: str
