"""Shared ledger with registered-author transaction ownership."""

from collections.abc import Mapping
from dataclasses import dataclass

from .locked_store import BusLockFile
from .errors import UnregisteredThreadError
from .registration import Registration
from .shared_ledger import SharedLedger
from .store_files import _store_lock


@dataclass(frozen=True, slots=True)
class CollaborationLedger(SharedLedger):
    registry: Registration

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        with _store_lock(BusLockFile.path_in(self.path.parent)):
            if author not in self.registry:
                raise UnregisteredThreadError(f"Author {author!r} is not registered.")
            super().merge(updates, author)
