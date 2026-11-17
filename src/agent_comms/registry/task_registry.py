"""The original live namespace reads consumed by authored task declarations."""

from abc import ABC, abstractmethod

from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.task_owner import TaskOwner


class ClaimGenerationRead(ABC):
    """Reads the current claim generation of a name."""

    __slots__ = ()

    @abstractmethod
    def claim_generation(self, name: str) -> int: ...


class TaskRegistry[Owner: TaskOwner](ClaimGenerationRead, RegistryNames[Owner], ABC):
    __slots__ = ()
