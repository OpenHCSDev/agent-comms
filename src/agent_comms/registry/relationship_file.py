from __future__ import annotations

from agent_comms.storage.locked_store import DurableFile


class RelationshipFile(DurableFile):
    """Where the durable relationships document lives under a bus root. The store
    inherits it; readers that need only the path import this, not the store."""

    filename = "relationships.json"
