"""Original claim-generation read contract, decoupled from task-owner namespaces."""

from abc import ABC, abstractmethod


class ClaimGenerationRead(ABC):
    """Reads the current claim generation of a name."""

    __slots__ = ()

    @abstractmethod
    def claim_generation(self, name: str) -> int: ...
