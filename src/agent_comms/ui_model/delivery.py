"""What an owner's input delivery shows: inputs without a confirmed start, and earlier notices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_comms.field_codec import FieldCodec


@dataclass(frozen=True)
class InputDelivery:
    """The owner's delivery overview (``InputDocument.delivery_overview``), decoded once.

    ``owner_queue`` says the current inputs were checked against the live
    owner's turn and queue, so they await a start; otherwise they are only
    unconfirmed. Rows are the owner's public input records.
    """

    inputs: tuple[dict[str, Any], ...] = ()
    historical_count: int = 0
    dismissed_historical_count: int = 0
    historical_inputs: tuple[dict[str, Any], ...] = ()
    owner_queue: bool = False

    SCOPES = {"owner_queue": True, "unobserved": False}

    @classmethod
    def from_wire(cls, overview: Mapping[str, Any]) -> InputDelivery:
        expected = {"inputs", "historicalCount", "dismissedHistoricalCount", "historicalInputs", "currentScope"}
        if set(overview) != expected:
            raise ValueError(f"Delivery overview fields {sorted(overview)} are not {sorted(expected)}")
        return cls(
            FieldCodec.decode(tuple[dict[str, Any], ...], overview["inputs"]),
            FieldCodec.decode(int, overview["historicalCount"]),
            FieldCodec.decode(int, overview["dismissedHistoricalCount"]),
            FieldCodec.decode(tuple[dict[str, Any], ...], overview["historicalInputs"]),
            cls.SCOPES[overview["currentScope"]],
        )

    @property
    def current(self) -> int:
        return len(self.inputs)

    @property
    def notices(self) -> int:
        """Earlier notices still shown, else those cleared."""
        return self.historical_count or self.dismissed_historical_count

    @property
    def shown(self) -> bool:
        return bool(self.current or self.historical_count or self.dismissed_historical_count)

    @property
    def pending(self) -> str:
        return "awaiting start" if self.owner_queue else "unconfirmed"

    @property
    def notice_label(self) -> str:
        return "earlier notices" if self.owner_queue else "historical notices"

    def history_changed(self, other: InputDelivery) -> bool:
        return (self.historical_count, self.dismissed_historical_count) != (
            other.historical_count, other.dismissed_historical_count)
