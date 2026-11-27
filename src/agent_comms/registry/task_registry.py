"""The original live namespace reads consumed by authored task declarations."""

from abc import ABC

from agent_comms.registry.claim_generation_read import ClaimGenerationRead
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.task_owner import TaskOwner


class TaskRegistry[Owner: TaskOwner](RegistryNames[Owner], ClaimGenerationRead, ABC):
    __slots__ = ()
