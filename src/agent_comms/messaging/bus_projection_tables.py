"""Base schema family for bus projection tables, usable without importing Message or Bus."""

from __future__ import annotations

from agent_comms.storage.typed_table import DerivedTables, ProducedTable, SharedFileTables


class BusDerivedTables(ProducedTable, SharedFileTables, DerivedTables, declared_name=None):
    """Derived reverse-lookup tables written in the bus append transaction."""
