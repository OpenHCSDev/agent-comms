"""The original common observation base, below concrete events and turn phases."""


class AgentEvent:
    """Identity and data for one observation, with its constructor owned by its dataclass."""

    def require_preparation_success(self) -> None:
        pass

    def retain_terminal(self, turn) -> None:
        pass
