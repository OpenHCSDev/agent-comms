"""Unread-count read contract consumed by thread views without importing bus snapshot machinery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence


class PendingCountSource(ABC):
    __slots__ = ()

    @abstractmethod
    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]: ...
