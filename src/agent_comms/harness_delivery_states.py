"""What one harness delivery attempt established: its outcome family.

Kept below the coordinator tables in the import graph so the table that records
attempts can declare these families without importing the bridge transport.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .field_codec import TranscriptRecordId

if TYPE_CHECKING:
    from .assignment_states import AssignmentState
    from .assignment_store import AssignmentStore
    from .cli_bridges.shared import LineMonitorBridge, MonitorPart, MonitorReadFile
    from .coordination_tables.harness_deliveries import HarnessDelivery
    from .notification_progress import DeliveryProgress


class Outcome(DeclaredFamily, affix="Outcome"):
    """Stored spellings stay the same; the stored outcome decides what may follow it."""

    @classmethod
    def claim_steps(cls, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        """The claim steps this outcome proves; an attempt that proves nothing moves no claim."""
        return ()

    @classmethod
    def settle(cls, successor: HarnessDelivery, assignments: AssignmentStore,
               db: sqlite3.Connection) -> HarnessDelivery:
        """An open attempt takes the successor and the claim steps its outcome proves."""
        successor.record(assignments, db)
        return successor

    @classmethod
    def consumed_progress(cls, progress: DeliveryProgress) -> DeliveryProgress:
        """The recipient's progress after this attempt; an emitted, unconfirmed or filtered
        attempt proves no context entry and leaves it."""
        return progress

    @classmethod
    def finished_with(cls, export: MonitorReadFile) -> None:
        """An unconsumed body keeps its export for a later Read."""


class FilteredOutcome(Outcome):
    @classmethod
    def settle(cls, successor: HarnessDelivery, assignments: AssignmentStore,
               db: sqlite3.Connection) -> HarnessDelivery:
        """A settled attempt stays settled; a later emission or observation cannot change it."""
        stored, = successor.stored(db)
        return stored


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    @classmethod
    def claim_steps(cls, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        return state.harness_delivery()

    @classmethod
    def consumed_progress(cls, progress: DeliveryProgress) -> DeliveryProgress:
        return progress.harness_consumed()

    @classmethod
    def finished_with(cls, export: MonitorReadFile) -> None:
        export.retire()


class EmittedOutcome(UnconfirmedOutcome):
    pass


@dataclass(frozen=True)
class MonitorEvidence(DeclaredFamily):
    """Unsent and terminal observations cannot accept another delivery."""

    async def confirm(self, bridge: LineMonitorBridge, delivery: HarnessDelivery,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[str, ...]:
        return ()

    def evidence(self, recorded: type[Outcome]) -> tuple[type[Outcome], MonitorEvidence]:
        """The outcome this observation proves, with the observation that proves it; one that
        proves nothing leaves the recorded outcome."""
        return recorded, self


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
        await bridge.finish(delivery, partial(replace, outcome=DeliveredOutcome, monitor=record))
        return ((message.exact_reply_target,)
                if message.role.response_policy(message).starts_turn_for(message, bridge.binding.bus)
                else ())


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(MonitorEvidence):
    record_id: TranscriptRecordId
    timestamp: float

    def evidence(self, recorded: type[Outcome]) -> tuple[type[Outcome], MonitorEvidence]:
        """Only a complete, matching confirmation stores this, so the message entered context."""
        return DeliveredOutcome, self
