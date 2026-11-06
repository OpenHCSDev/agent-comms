"""When each registered thread was last seen: presence, kept beside the registry.

A heartbeat changes nothing about a thread's declaration, only when it was last seen. Kept in
registry.json, every heartbeat rewrote the whole registry (5.7 MB on the live bus) and made
every reader decode a new revision. The registry edit that changes a thread's ``last_seen``
writes it here instead, before the registry itself, so a reader that sees a thread sees its
presence too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from .nominal.field_codec import SingleFieldValue
from .storage.locked_store import DocumentChange
from .storage.maintenance_barrier import CarriedDocument
from .storage.store_files import DependentStateTier, SingleFileStore


@dataclass(frozen=True)
class PresenceDocument(SingleFieldValue):
    """Each thread name's last-seen time, keyed as the registry keys its rows."""

    seen: dict[str, float] = field(default_factory=dict)

    def setting(self, after: Mapping[str, float], before: Mapping[str, float]) -> PresenceDocument:
        """This document with the rows one registry edit set or changed."""
        return PresenceDocument({**self.seen, **{
            name: when for name, when in after.items() if before.get(name) != when}})

    def without(self, names: frozenset[str]) -> PresenceDocument:
        return PresenceDocument({name: when for name, when in self.seen.items() if name not in names})


class PresenceStore(SingleFileStore, CarriedDocument[PresenceDocument], DependentStateTier):
    """Registered threads' presence, committed by the registry edit that changes it."""

    filename: ClassVar[str] = "presence.json"

    record_type = PresenceDocument

    def empty(self) -> PresenceDocument:
        return PresenceDocument()

    def set_rows(self, after: Mapping[str, float], before: Mapping[str, float]) -> None:
        """Before the registry is saved: every revision a reader can see, the old one and the
        new one, has a presence row for each thread it names."""
        self.update(lambda document: DocumentChange.to(document.setting(after, before)))

    def drop_rows(self, names: frozenset[str]) -> None:
        """After the registry is saved: rows of names it no longer declares."""
        self.update(lambda document: DocumentChange.to(document.without(names)))
