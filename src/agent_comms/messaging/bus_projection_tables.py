"""Base schema family for bus projection tables, usable without importing Message or Bus."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.messaging.message_sender import PublicSender
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.storage.typed_table import DerivedTables, ProducedTable, SharedFileTables


class BusDerivedTables(ProducedTable, SharedFileTables, DerivedTables, declared_name=None):
    """Derived reverse-lookup tables written in the bus append transaction."""


@dataclass(frozen=True)
class BusRouteIdentity(BusDerivedTables, declared_name=None):
    """Shared route-key columns for bus route-counting and route-index tables."""

    target: str
    sender: str
    sender_lookup: ThreadLookup | PublicSender
