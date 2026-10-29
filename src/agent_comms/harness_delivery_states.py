"""What one harness delivery attempt established: its outcome family.

Kept below the coordinator tables in the import graph so the table that records
attempts can declare these families without importing the bridge transport.
"""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
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
    from .presentation import MessageNotification


class Outcome(DeclaredFamily, affix="Outcome"):
    """Stored spellings stay the same; the stored outcome decides what may follow it."""

    @classmethod
    def settle(cls, successor: HarnessDelivery, assignments: AssignmentStore,
               db: sqlite3.Connection) -> HarnessDelivery:
        """An open attempt takes the successor and the claim steps its outcome proves."""
        successor.record(assignments, db)
        return successor

    @classmethod
    def arrival_count(cls) -> int:
        return 0

    @classmethod
    def arrival_label(cls, notification: MessageNotification) -> str:
        return "Arrival unknown"

    @classmethod
    def input_detail(cls) -> str:
        return "Attempt is uncertain; no replay is permitted."


class FilteredOutcome(Outcome):
    @classmethod
    def input_detail(cls) -> str:
        return "No input was submitted."

    @classmethod
    def settle(cls, successor: HarnessDelivery, assignments: AssignmentStore,
               db: sqlite3.Connection) -> HarnessDelivery:
        """A settled attempt stays settled; a later emission or observation cannot change it."""
        stored, = successor.stored(db)
        return stored


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    """An original harness receipt, not evidence that a model took the body.

    Older generic receipts retain this spelling and their actual monitor proof.
    A producer name alone cannot upgrade their evidence to model context.
    """

    @classmethod
    def input_detail(cls) -> str:
        return "Harness reported delivery; model context is unproved."


class AcceptedOutcome(DeliveredOutcome):
    @classmethod
    def arrival_count(cls) -> int:
        return 1

    @classmethod
    def arrival_label(cls, notification: MessageNotification) -> str:
        return "Arrived (RPC accepted; time unrecorded)"

    @classmethod
    def input_detail(cls) -> str:
        return "RPC accepted input; model context proof is unavailable."


class PersistedOutcome(AcceptedOutcome):
    @classmethod
    def arrival_label(cls, notification: MessageNotification) -> str:
        return "Arrived (saved input; time unrecorded)"

    @classmethod
    def input_detail(cls) -> str:
        return "Harness saved input; model context proof is unavailable."


class EmittedOutcome(UnconfirmedOutcome):
    @classmethod
    def input_detail(cls) -> str:
        return "Emitted; recipient context remains unconfirmed."


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

    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress:
        return progress

    def claim_steps(self, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        return ()

    def arrival_count(self, outcome: type[Outcome]) -> int:
        return outcome.arrival_count()

    def arrival_label(self, outcome: type[Outcome], notification: MessageNotification) -> str:
        return outcome.arrival_label(notification)

    def finished_with(self, export: MonitorReadFile) -> None:
        """A body without a complete original transcript proof keeps its export."""


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


class PendingMonitor(MonitorEvidence, declared_name=None):
    """The emission owns whether its later observation can prove the original body."""

    @classmethod
    @abstractmethod
    def for_parts(cls, total: int) -> PendingMonitor: ...


@dataclass(frozen=True)
class WakeOnlyMonitor(PendingMonitor):
    """A board wake is not the original packet, even if its bytes happen to match."""

    @classmethod
    def for_parts(cls, total: int) -> WakeOnlyMonitor:
        return cls()

    async def confirm(self, bridge: LineMonitorBridge, delivery: HarnessDelivery,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[str, ...]:
        # Retire only the wake's transport reservation, never original-body proof.
        await bridge.part_observed(delivery.message_id, notification.number)
        return ()


@dataclass(frozen=True)
class ReceivingMonitor(PendingMonitor):
    total: int

    @classmethod
    def for_parts(cls, total: int) -> ReceivingMonitor:
        return cls(total)

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

    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress:
        return progress.harness_consumed(round(self.timestamp * 1000))

    def claim_steps(self, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        return state.harness_delivery()

    def arrival_count(self, outcome: type[Outcome]) -> int:
        return 1

    def arrival_label(self, outcome: type[Outcome], notification: MessageNotification) -> str:
        return notification.timed_state("Arrived", round(self.timestamp * 1000))

    def finished_with(self, export: MonitorReadFile) -> None:
        export.retire()
