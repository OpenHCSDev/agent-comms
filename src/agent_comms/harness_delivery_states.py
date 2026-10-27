"""What one harness delivery attempt established: its outcome family.

Kept below the coordinator tables in the import graph so the table that records
attempts can declare these families without importing the bridge transport.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .field_codec import TranscriptRecordId

if TYPE_CHECKING:
    from .assignment_states import AssignmentState
    from .cli_bridges.shared import CliBridge, LineMonitorBridge, MonitorPart
    from .coordination_tables.harness_deliveries import HarnessDelivery


class Outcome(DeclaredFamily, affix="Outcome"):
    """Stored spellings stay the same; their owners perform confirmation."""

    @classmethod
    async def record(cls, bridge: CliBridge, delivery: HarnessDelivery) -> None:
        await bridge.coordinator_write(replace(delivery, outcome=cls).record)

    @classmethod
    def claim_steps(cls, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        """The claim steps this outcome proves; an attempt that proves nothing moves no claim."""
        return ()

    @classmethod
    async def finish(
        cls, bridge: CliBridge, delivery: HarnessDelivery, observation: type[Outcome]
    ) -> None:
        await observation.record(bridge, delivery)

    @classmethod
    def entered_context(cls) -> bool:
        """Whether this attempt proves the message entered the harness context; an emitted,
        unconfirmed or filtered attempt proves nothing."""
        return False


class FilteredOutcome(Outcome):
    @classmethod
    async def finish(
        cls, bridge: CliBridge, delivery: HarnessDelivery, observation: type[Outcome]
    ) -> None:
        """The journal confirmed this row; a later emission cannot change that fact."""


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    @classmethod
    def claim_steps(cls, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        return state.harness_delivery()

    @classmethod
    def entered_context(cls) -> bool:
        return True


class EmittedOutcome(UnconfirmedOutcome):
    pass


@dataclass(frozen=True)
class MonitorEvidence(DeclaredFamily):
    """Unsent and terminal observations cannot accept another delivery."""

    async def confirm(self, bridge: LineMonitorBridge, delivery: HarnessDelivery,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


@dataclass(frozen=True)
class ReceivingMonitor(MonitorEvidence):
    total: int

    async def confirm(self, bridge: LineMonitorBridge, delivery: HarnessDelivery,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[str, ...]:
        if notification.total != self.total:
            return ()
        parts = await bridge.observe_part(delivery, notification, record)
        if len(parts) != self.total:
            return ()
        message = await bridge.message(delivery.reference)
        if (''.join(part.body for part in parts) != message.body
                or any(part.sender != message.sender for part in parts)):
            await bridge.record_mismatch(delivery, 'Observed parts do not equal the original message')
            return ()
        await bridge.finish(replace(delivery, monitor=record), DeliveredOutcome)
        return (message.exact_reply_target,)


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(MonitorEvidence):
    record_id: TranscriptRecordId
    timestamp: float
