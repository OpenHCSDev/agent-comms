"""What an owner's input delivery shows: inputs without a confirmed start, and earlier notices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pydantic import JsonValue

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import HistoricalInput, UnresolvedInput
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.thread_presentation import ThreadPresentation, ThreadPresentationStatus


class InputDeliveryReadState(DeclaredFamily, affix="InputDeliveryReadState"):
    """One delivery read's availability, without inventing a new input outcome."""

    error = ""

    def present_status(self, status: ThreadPresentationStatus) -> ThreadPresentationStatus:
        return status


@dataclass(frozen=True)
class LoadedInputDeliveryReadState(InputDeliveryReadState):
    pass


@dataclass(frozen=True)
class FailedInputDeliveryReadState(InputDeliveryReadState):
    error: str = field()

    def present_status(self, status: ThreadPresentationStatus) -> ThreadPresentationStatus:
        return status.with_attention(self.error)


@dataclass(frozen=True)
class InputDelivery:
    """The owner's delivery overview (``InputDocument.delivery_overview``), decoded once.

    ``owner_queue`` says the current inputs were checked against the live
    owner's turn and queue, so they await a start; otherwise they are only
    unconfirmed. Rows are the owner's public input records.
    """

    inputs: tuple[UnresolvedInput, ...] = ()
    historical_count: int = 0
    dismissed_historical_count: int = 0
    historical_inputs: tuple[HistoricalInput, ...] = ()
    owner_queue: bool = False
    read_state: InputDeliveryReadState = LoadedInputDeliveryReadState()

    @property
    def error(self) -> str:
        return self.read_state.error

    def failed(self, error: Exception) -> InputDelivery:
        """Retain the last acquired input evidence when its refresh fails."""
        return replace(self, read_state=FailedInputDeliveryReadState(str(error)))

    def presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        """Pending input or a failed read warns without erasing genuine work."""
        status = ordinary.status.with_attention(self.summary) if self.current else ordinary.status
        return self.read_state.present_status(status).present_attention(ordinary)

    @classmethod
    def from_wire(cls, overview: Mapping[str, JsonValue]) -> InputDelivery:
        decoded = FieldCodec.decode(DeliveryOverview, overview)
        return cls(
            decoded.inputs,
            decoded.historical_count,
            decoded.dismissed_historical_count,
            decoded.historical_inputs,
            decoded.current_scope.owner_queue,
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
    def summary(self) -> str:
        """Waiting input jobs are not per-recipient message totals."""
        return f"Delivery · {self.current} {self.pending}"

    @property
    def pending(self) -> str:
        return "awaiting start" if self.owner_queue else "unconfirmed"

    @property
    def notice_label(self) -> str:
        return "earlier notices" if self.owner_queue else "historical notices"

    def history_changed(self, other: InputDelivery) -> bool:
        return (self.historical_count, self.dismissed_historical_count) != (
            other.historical_count, other.dismissed_historical_count)
