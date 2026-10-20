"""The public historical-message decoration, not a source or delivery authority."""
from dataclasses import dataclass

from .recorded_bus import RecordedBusIdentity


@dataclass(frozen=True)
class HistoryMessageProvenance:
    source: str
    wire_root_id: str
    bus_identity: RecordedBusIdentity
    sender_created_at: float | None
    target_created_at: float | None
