from __future__ import annotations

from agent_comms.storage.locked_store import DurableFile, PublishedSQLiteFile


class TodoStoreFile(DurableFile, PublishedSQLiteFile):
    """Where the durable todo store lives under a bus root. TodoStore inherits it;
    readers that need only the path import this, not the orders module."""

    __slots__ = ()

    filename = "todos.sqlite3"
