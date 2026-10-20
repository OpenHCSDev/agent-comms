"""Public history provenance is explicitly absent or source-recorded."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, ClassVar

from .field_codec import FieldCodec, NullMember, RecordMember, SubclassUnion
from .recorded_bus import RecordedBusIdentity
from .recorded_time import EventTime, EventTimeWire


class HistoryProvenance(SubclassUnion, affix="HistoryProvenance"):
    envelope_key: ClassVar[str] = "history"

    @abstractmethod
    def metadata(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class NoHistoryProvenance(NullMember, HistoryProvenance):
    def metadata(self) -> dict[str, object]:
        return {}


@dataclass(frozen=True)
class HistoryMessageProvenance(RecordMember, HistoryProvenance):
    source: str
    wire_root_id: str
    bus_identity: RecordedBusIdentity
    sender_created_at: Annotated[EventTime, EventTimeWire]
    target_created_at: Annotated[EventTime, EventTimeWire]

    def metadata(self) -> dict[str, object]:
        return {self.envelope_key: FieldCodec.encode(self)}
