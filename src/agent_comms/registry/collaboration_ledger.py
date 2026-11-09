"""Shared ledger with registered-author transaction ownership."""

from collections.abc import Mapping
from dataclasses import dataclass

from agent_comms.errors import UnregisteredThreadError
from agent_comms.registry.registration import Registration
from agent_comms.registry.shared_ledger import SharedLedger


@dataclass(frozen=True, slots=True)
class CollaborationLedger(SharedLedger):
    registry: Registration

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        with self.registry.store.current_snapshot() as cut:
            registry = cut.snapshot
            if registry.canonical_name(author) not in registry.threads:
                raise UnregisteredThreadError(f"Author {author!r} is not registered.")
            super().merge(updates, author)
