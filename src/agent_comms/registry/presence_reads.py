"""Original participant/process facts borrowed by the presence family."""
from __future__ import annotations
from abc import ABC, abstractmethod

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.planning.goal_failure_owner import ProcessOwner
from agent_comms.registry.thread_identity import OwnerIdentity, ThreadIncarnation, ThreadRole


class PresenceThread(ProcessOwner, ABC):
    name: str
    role: type[ThreadRole]

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...

    @property
    @abstractmethod
    def process_alive(self) -> bool: ...

    @abstractmethod
    def require_process(self) -> ProcessIdentity: ...


class PresenceEntry(ABC):
    __slots__ = ()
    thread: PresenceThread
    owner_generation: int

    @abstractmethod
    def matches_name(self, name: str) -> bool: ...


class PresenceSnapshot(ABC):
    __slots__ = ()

    @abstractmethod
    def owner_identity(self, name: str) -> OwnerIdentity: ...
