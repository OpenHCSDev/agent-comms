"""Read operations used by thread declarations, independent of their storage owners."""

from pathlib import Path
from typing import Protocol, Self

from ..projects.checkouts import RepositoryReads
from ..thread_presentation import ThreadOwnerBinding


class ThreadCheckoutSource[Owner](Protocol):
    def checkouts_of(self, thread: Owner, reads: RepositoryReads) -> tuple[Path, ...]: ...


class ThreadOwnerStatus[Snapshot, Owner](Protocol):
    def native_owner_binding(self, snapshot: Snapshot, thread: Owner) -> ThreadOwnerBinding: ...


class ThreadOwnerSource[Owner](Protocol):
    def status(self, name: str) -> ThreadOwnerStatus[Self, Owner]: ...
