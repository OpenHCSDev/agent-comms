"""The producer's item identity within its native turn."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnItem:
    turn_id: str
    item_id: str

    @property
    def key(self) -> TurnItem:
        return TurnItem(self.turn_id, self.item_id)
