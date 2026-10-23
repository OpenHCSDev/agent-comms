"""What an owner's input delivery shows: inputs without a confirmed start, and earlier notices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pydantic import JsonValue

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
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
class DeliveryView:
    """The owner's decoded delivery overview and this view's read state."""

    overview: DeliveryOverview
    read_state: InputDeliveryReadState = LoadedInputDeliveryReadState()

    @property
    def error(self) -> str:
        return self.read_state.error

    def failed(self, error: Exception) -> DeliveryView:
        """Retain the last acquired input evidence when its refresh fails."""
        return replace(self, read_state=FailedInputDeliveryReadState(str(error)))

    def presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        """Pending input or a failed read warns without erasing genuine work."""
        status = ordinary.status.with_attention(self.summary) if self.current else ordinary.status
        return self.read_state.present_status(status).present_attention(ordinary)

    @classmethod
    def from_wire(cls, overview: Mapping[str, JsonValue]) -> DeliveryView:
        return cls(FieldCodec.decode(DeliveryOverview, overview))

    @property
    def current(self) -> int:
        return len(self.overview.inputs)

    @property
    def notices(self) -> int:
        """Earlier notices still shown, else those cleared."""
        return self.overview.historical_count or self.overview.dismissed_historical_count

    @property
    def shown(self) -> bool:
        return bool(
            self.current or self.overview.historical_count or self.overview.dismissed_historical_count
        )

    @property
    def summary(self) -> str:
        """Waiting input jobs are not per-recipient message totals."""
        return f"Delivery · {self.current} {self.pending}"

    @property
    def pending(self) -> str:
        return self.overview.current_scope.pending_label

    @property
    def notice_label(self) -> str:
        return self.overview.current_scope.notices_label

    def history_changed(self, other: DeliveryView) -> bool:
        return (self.overview.historical_count, self.overview.dismissed_historical_count) != (
            other.overview.historical_count, other.overview.dismissed_historical_count)
