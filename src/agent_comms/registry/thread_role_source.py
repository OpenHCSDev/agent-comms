"""Original authorship role, independent of thread runtime assembly."""

from abc import ABC

from agent_comms.registry.thread_identity import ThreadRole


class ThreadRoleSource(ABC):
    __slots__ = ()

    role: type[ThreadRole]
