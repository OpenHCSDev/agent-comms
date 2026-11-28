"""Compaction rule-deal callback contract, below concrete rule-deal storage."""

from __future__ import annotations

from abc import ABC, abstractmethod


class CompactionRedeal(ABC):
    """Rule-deal capability invoked when a compaction places its summary."""

    @abstractmethod
    def redeal_compacted(self, compacted_at: str) -> None: ...
