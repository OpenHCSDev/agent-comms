"""When each registered thread was last seen: presence, kept beside the registry.

A heartbeat changes nothing about a thread's declaration, only when it was last seen. Kept in
registry.json, every heartbeat rewrote the whole registry (5.7 MB on the live bus) and made
every reader decode a new revision. The registry edit that changes a thread's ``last_seen``
writes it here instead, before the registry itself.

A registry edit sets rows and never drops them. A reader lays presence over a registry revision
it captured earlier, possibly before a later edit removed or renamed a thread; because rows only
ever arrive, every revision a reader can hold names only threads that still have a row. A name
that left the registry keeps its few bytes here until it is declared again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from agent_comms.nominal.field_codec import SingleFieldValue
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.maintenance_barrier import CarriedDocument
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore


@dataclass(frozen=True)
class PresenceDocument(SingleFieldValue):
    """Each thread name's last-seen time, keyed as the registry keys its rows."""

    seen: dict[str, float] = field(default_factory=dict)

    def setting(self, after: Mapping[str, float], before: Mapping[str, float]) -> PresenceDocument:
        """This document with the rows one registry edit set or changed."""
        return PresenceDocument({**self.seen, **{
            name: when for name, when in after.items() if before.get(name) != when}})


class PresenceStore(SingleFileStore, CarriedDocument[PresenceDocument], DependentStateTier):
    """Registered threads' presence, committed by the registry edit that changes it."""

    filename: ClassVar[str] = "presence.json"

    record_type = PresenceDocument

    def empty(self) -> PresenceDocument:
        return PresenceDocument()

    def set_rows(self, after: Mapping[str, float], before: Mapping[str, float]) -> None:
        """Before the registry is saved, so the revision it writes names no thread without a
        row; earlier revisions never lose theirs."""
        self.update(lambda document: DocumentChange.to(document.setting(after, before)))
