"""What one harness delivery attempt established: its outcome family.

Kept below the coordinator tables in the import graph so the table that records
attempts can declare these families without importing the bridge transport.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from agent_comms.execution.turn_item import NativeItemId, NativeTurnId
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_evidence import ConsumptionEvidence
from agent_comms.messaging.notification_progress import HarnessConsumedProgress
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import TranscriptRecordId
from agent_comms.storage.recorded_time import Timestamped

if TYPE_CHECKING:
    from agent_comms.cli_bridges.shared import (
        MonitorPart,
        MonitorReadFile,
        TranscriptConfirmsDelivery,
    )
    from agent_comms.coordination.assignment_states import AssignmentState
    from agent_comms.coordination.assignment_store import AssignmentStore
    from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
    from agent_comms.messaging.notification_progress import DeliveryProgress


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


class AcceptedOutcome(Outcome):
    pass


class PersistedOutcome(AcceptedOutcome):
    pass


class EmittedOutcome(UnconfirmedOutcome):
    pass


@dataclass(frozen=True)
class MonitorEvidence(ConsumptionEvidence, DeclaredFamily):
    """Unsent and terminal observations cannot accept another delivery."""

    async def confirm(self, bridge: TranscriptConfirmsDelivery, delivery: HarnessDelivery,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[MessageReference, ...]:
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

    def taken_under(self, ledger: TakingLedger) -> bool:
        """Only a cloud watch's attempt names the ledger that took it; nothing else is replayed."""
        return False


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


@dataclass(frozen=True)
class ReceivingMonitor(MonitorEvidence):
    total: int

    async def confirm(self, bridge: TranscriptConfirmsDelivery, delivery: HarnessDelivery,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[MessageReference, ...]:
        if notification.total != self.total:
            return ()
        parts = await bridge.observe_part(delivery, notification, record)
        if len(parts) != self.total:
            return ()
        message = await bridge.message(delivery.reference)
        if message is None:
            return ()
        if (''.join(part.body for part in parts) != message.body
                or any(part.sender != message.sender for part in parts)):
            await bridge.record_mismatch(delivery, 'Observed parts do not equal the original message')
            return ()
        await bridge.finish(delivery, partial(replace, outcome=DeliveredOutcome, monitor=record))
        return message.opening_references(bridge.transcript_thread)


class TakingLedger:
    """No ledger took this attempt: an old client's, or one taken before its ledger arrived.
    Its evidence names nothing, so nothing can prove it unshown and it is never sent again."""

    def receiving(self, total: int) -> MonitorEvidence:
        return ReceivingMonitor(total)


@dataclass(frozen=True)
class PrintLedgerId(TakingLedger):
    """One client's printed ledger, born with its file: a ledger in a fresh container is
    another ledger, so its silence proves nothing about what the old one took."""

    token: str

    def receiving(self, total: int) -> MonitorEvidence:
        return LedgerReceivingMonitor(total, self)


@dataclass(frozen=True)
class TakenUnderLedger(MonitorEvidence):
    ledger: PrintLedgerId

    def taken_under(self, ledger: TakingLedger) -> bool:
        return self.ledger == ledger


@dataclass(frozen=True)
class LedgerReceivingMonitor(TakenUnderLedger, ReceivingMonitor):
    """A message a cloud watch took under this ledger, awaiting every part: its one inline
    event, or each slice a Read of its export shows."""


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


class ObservedInputEvidence(MonitorEvidence, Timestamped, declared_name=None):
    """A complete original input observed in its harness, with its source retained."""

    def evidence(self, recorded: type[Outcome]) -> tuple[type[Outcome], MonitorEvidence]:
        """Only a complete, matching confirmation stores this, so the message entered context."""
        return DeliveredOutcome, self

    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress:
        return progress.model_consumed(HarnessConsumedProgress(self.at_ms))

    def claim_steps(self, state: AssignmentState) -> tuple[type[AssignmentState], ...]:
        return state.harness_delivery()

    def finished_with(self, export: MonitorReadFile) -> None:
        export.retire()

@dataclass(frozen=True)
class ObservedMonitor(ObservedInputEvidence):
    record_id: TranscriptRecordId
    timestamp: float


@dataclass(frozen=True)
class ObservedBridgeInput(ObservedInputEvidence):
    turn_id: NativeTurnId
    item_id: NativeItemId
    timestamp: float
