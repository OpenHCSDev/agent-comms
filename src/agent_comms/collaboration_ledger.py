"""Shared ledger with registered-author transaction ownership."""

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import UnregisteredThreadError
from .registration import Registration
from .shared_ledger import SharedLedger


@dataclass(frozen=True, slots=True)
class CollaborationLedger(SharedLedger):
    registry: Registration

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        with self.registry.store.reading_snapshot() as registry:
            if registry.canonical_name(author) not in registry.threads:
                raise UnregisteredThreadError(f"Author {author!r} is not registered.")
            super().merge(updates, author)
