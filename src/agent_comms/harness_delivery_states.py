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
    from .cli_bridges.shared import LineMonitorBridge, MonitorPart
    from .coordination_tables.harness_deliveries import HarnessDelivery


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
    def entered_context(cls) -> bool:
        """Whether this attempt proves the message entered the harness context; an emitted,
        unconfirmed or filtered attempt proves nothing."""
        return False


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

    def proven_outcome(self, recorded: type[Outcome]) -> type[Outcome]:
        """The outcome this observation proves; one that proves nothing leaves the recorded one."""
        return recorded


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
        return (message.exact_reply_target,)


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(MonitorEvidence):
    record_id: TranscriptRecordId
    timestamp: float

    def proven_outcome(self, recorded: type[Outcome]) -> type[Outcome]:
        """Only a complete, matching confirmation stores this, so the message entered context."""
        return DeliveredOutcome
