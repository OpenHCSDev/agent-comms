"""Lookup on the original input provider, distinct from ordered batch capture."""

from abc import ABC, abstractmethod

from agent_comms.execution.input_key import InputKey


class InputLookup[Attempt](ABC):
    __slots__ = ()

    @abstractmethod
    def lookup(self, key: InputKey) -> Attempt: ...
