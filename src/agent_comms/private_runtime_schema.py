"""Explicit private-runtime installation capability for schema declarations."""

from __future__ import annotations

from abc import ABC, abstractmethod


class PrivateRuntimeSchema(ABC):
    """Installation capability on the canonical private schema declarations."""

    @classmethod
    @abstractmethod
    def install(cls, store) -> None:
        """Install absent current state; reject drift without repairing it."""
