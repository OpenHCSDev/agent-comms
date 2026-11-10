"""Read operations used by thread declarations, independent of their storage owners."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Self

from agent_comms.projects.checkouts import RepositoryReads
from agent_comms.registry.thread_presentation import ThreadOwnerBinding


class ThreadCheckoutSource[Owner](ABC):
    __slots__ = ()

    @abstractmethod
    def checkouts_of(self, thread: Owner, reads: RepositoryReads) -> tuple[Path, ...]: ...


class ThreadOwnerStatus[Snapshot, Owner](ABC):
    __slots__ = ()

    @abstractmethod
    def native_owner_binding(self, snapshot: Snapshot, thread: Owner) -> ThreadOwnerBinding: ...


class ThreadOwnerSource[Owner](ABC):
    __slots__ = ()

    @abstractmethod
    def status(self, name: str) -> ThreadOwnerStatus[Self, Owner]: ...
