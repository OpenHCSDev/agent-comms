"""The original ordered input rows a captured batch requires."""

from abc import ABC, abstractmethod

from agent_comms.input_attempt import StoredInput
from agent_comms.input_key import InputKey


class OriginalInputs(ABC):
    __slots__ = ()

    @abstractmethod
    def originals(self, keys: tuple[InputKey, ...]) -> tuple[StoredInput, ...]:
        """Require each original key once and retain the requested order."""
        ...
