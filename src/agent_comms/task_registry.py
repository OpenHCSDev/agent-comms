"""The original live namespace reads consumed by authored task declarations."""

from abc import abstractmethod

from agent_comms.registry_names import RegistryNames
from agent_comms.task_owner import TaskOwner


class TaskRegistry[Owner: TaskOwner](RegistryNames[Owner]):
    __slots__ = ()

    @abstractmethod
    def claim_generation(self, name: str) -> int: ...
