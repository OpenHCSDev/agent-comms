"""Explicit private-runtime installation capability for schema declarations."""

from __future__ import annotations

from abc import abstractmethod

from .typed_table import DurableTables, SharedFileTables


class PrivateRuntimeSchema(SharedFileTables, DurableTables):
    """Durable private tables own both installation and stored-state discovery."""

    @classmethod
    @abstractmethod
    def install(cls, store) -> None:
        """Install absent current state; reject drift without repairing it."""
