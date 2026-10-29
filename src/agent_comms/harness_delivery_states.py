"""What one harness delivery attempt established: its outcome family.

Kept below the coordinator tables in the import graph so the table that records
attempts can declare these families without importing the bridge transport.
"""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from .channel_targets import CommandTarget
from .declared_family import DeclaredFamily
from .field_codec import TranscriptRecordId
from .notification_evidence import ConsumptionEvidence
from .recorded_time import Timestamped
from .notification_progress import HarnessConsumedProgress

if TYPE_CHECKING:
    from .assignment_states import AssignmentState
    from .assignment_store import AssignmentStore
    from .cli_bridges.shared import LineMonitorBridge, MonitorPart, MonitorReadFile
    from .coordination_tables.harness_deliveries import HarnessDelivery
    from .notification_progress import DeliveryProgress


class Outcome(DeclaredFamily, affix="Outcome"):
    """Stored spellings stay the same; the stored outcome decides what may follow it."""

    @classmethod
    def settle(cls, successor: HarnessDelivery, assignments: AssignmentStore,
               db: sqlite3.Connection) -> HarnessDelivery:
        """An open attempt takes the successor and the claim steps its outcome proves."""
        successor.record(assignments, db)
        return successor


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
    """An original harness response, not evidence that a model took the body.

    Older generic responses retain this spelling and their actual monitor proof.
    A producer name alone cannot upgrade their evidence to model context.
    """


class AcceptedOutcome(DeliveredOutcome):
    pass


class PersistedOutcome(AcceptedOutcome):
    pass


class EmittedOutcome(UnconfirmedOutcome):
    pass


@dataclass(frozen=True)
class MonitorEvidence(ConsumptionEvidence, DeclaredFamily):
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

    def finished_with(self, export: MonitorReadFile) -> None:
        """A body without a complete original transcript proof keeps its export."""


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


class PendingMonitor(MonitorEvidence, declared_name=None):
    """The emission owns whether its later observation can prove the original body."""

    @classmethod
    def displayed_body(cls, body: str) -> str:
        return body

    @classmethod
    @abstractmethod
    def for_parts(cls, total: int) -> PendingMonitor: ...


@dataclass(frozen=True)
class WakeOnlyMonitor(PendingMonitor):
    """A board wake is not the original packet, even if its bytes happen to match."""

    board_wake: ClassVar[str] = (
        "Tristan: your board changed. New and changed lines print once as BOARD: on your next "
        "tool call; later calls print one unchanged count."
    )

    @classmethod
    def displayed_body(cls, body: str) -> str:
        return cls.board_wake

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
        return (CommandTarget.named(message.target).automatic_reply_targets(message.sender)
                if message.role.response_policy(message).starts_turn_for(message, bridge.binding.bus)
                else ())


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(MonitorEvidence, Timestamped):
    record_id: TranscriptRecordId
    timestamp: float

    def evidence(self, recorded: type[Outcome]) -> tuple[type[Outcome], MonitorEvidence]:
        """Only a complete, matching confirmation stores this, so the message entered context."""
        return DeliveredOutcome, self

    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress:
        return progress.model_consumed(HarnessConsumedProgress(self.at_ms))

    def claim_steps(self, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        return state.harness_delivery()

    def finished_with(self, export: MonitorReadFile) -> None:
        export.retire()
