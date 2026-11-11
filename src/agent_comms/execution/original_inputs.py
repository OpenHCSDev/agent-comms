"""The original ordered input rows a captured batch requires."""

from abc import ABC, abstractmethod

from agent_comms.execution.input_key import InputKey
from agent_comms.input_attempt import StoredInput


class OriginalInputs(ABC):
    __slots__ = ()

    @abstractmethod
    def originals(self, keys: tuple[InputKey, ...]) -> tuple[StoredInput, ...]:
        """Require each original key once and retain the requested order."""
        ...
