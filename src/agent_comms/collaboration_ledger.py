"""Shared ledger with registered-author transaction ownership."""

from collections.abc import Mapping
from pathlib import Path

from .declarations import SharedLedger, UnregisteredThreadError, _store_lock
from .registration import Registration


class CollaborationLedger(SharedLedger):
    def __init__(self, root: Path, registry: Registration):
        super().__init__(root / "ledger.json")
        self.registry = registry
        self._wire_lock_path = root / "wire"

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        with _store_lock(self._wire_lock_path):
            if author not in self.registry:
                raise UnregisteredThreadError(f"Author {author!r} is not registered.")
            super().merge(updates, author)
