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


@dataclass(frozen=True, kw_only=True)
class DeliveryView:
    """No delivery overview has been read; failures still retain their own status."""

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
    def from_wire(cls, overview: Mapping[str, JsonValue]) -> ReadDeliveryView:
        return ReadDeliveryView(FieldCodec.decode(DeliveryOverview, overview))

    @property
    def current(self) -> int:
        return 0

    @property
    def notices(self) -> int:
        return 0

    @property
    def shown(self) -> bool:
        return False

    @property
    def summary(self) -> str:
        return ""

    @property
    def notices_summary(self) -> str:
        return ""

    @property
    def history_summary(self) -> str:
        return ""

    @property
    def dismissible_notices(self) -> bool:
        return False

    @staticmethod
    def records_text(inputs: tuple[UnresolvedInput, ...]) -> str:
        return "\n\n".join(row.record_text() for row in inputs)

    def pending_records_text(self) -> str:
        return ""

    def historical_records(self) -> tuple[HistoricalInput, ...]:
        return ()

    def history_changed(self, other: DeliveryView) -> bool:
        return False

    def history_changed_from(self, other: ReadDeliveryView) -> bool:
        return False


@dataclass(frozen=True)
class ReadDeliveryView(DeliveryView):
    """The delivery overview Core actually supplied, retained as one record."""

    overview: DeliveryOverview

    @property
    def current(self) -> int:
        return len(self.overview.inputs)

    @property
    def notices(self) -> int:
        """Earlier notices still shown, else those cleared."""
        return self.overview.historical_count or self.overview.dismissed_historical_count

    @property
    def shown(self) -> bool:
        return bool(self.current or self.notices)

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

    @property
    def notices_summary(self) -> str:
        count = (
            f"{self.overview.historical_count} {self.notice_label}"
            if self.overview.historical_count
            else f"{self.overview.dismissed_historical_count} {self.notice_label} cleared"
        )
        return f"{' · ' if self.current else ''}{count}"

    @property
    def history_summary(self) -> str:
        return (
            f"{self.overview.historical_count} {self.notice_label} · "
            f"{self.overview.dismissed_historical_count} cleared"
        )

    @property
    def dismissible_notices(self) -> bool:
        return bool(self.overview.historical_count)

    def pending_records_text(self) -> str:
        return self.records_text(self.overview.inputs) or "No inputs currently awaiting start."

    def historical_records(self) -> tuple[HistoricalInput, ...]:
        return self.overview.historical_inputs

    def history_changed(self, other: DeliveryView) -> bool:
        return other.history_changed_from(self)

    def history_changed_from(self, other: ReadDeliveryView) -> bool:
        return (self.overview.historical_count, self.overview.dismissed_historical_count) != (
            other.overview.historical_count, other.overview.dismissed_historical_count
        )
