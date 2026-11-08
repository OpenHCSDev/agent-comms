"""The original metadata writer supplied by an attached activity client."""

from abc import ABC
from typing import Protocol


class RuntimeInfoWrites[Observation](Protocol):
    def observe_runtime_info(self, thread: str, observation: Observation) -> None: ...


class RuntimeInfoSource[Observation](ABC):
    """A client exposes its existing activity writer, not a second store."""

    agents: RuntimeInfoWrites[Observation]
