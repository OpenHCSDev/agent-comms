"""Harness delivery attempts: what a bridge did with one bus message for one recipient.

The bus owns the message, so an attempt keeps only its reference. The attempt commits in
the same coordinator write as the claim step it causes, and the inbox reads consumption
from these rows beside the claims, from one store.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Self

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.coordination_tables.participants import Participants
from agent_comms.field_codec import MessageId, ThreadLookup
from agent_comms.harness_delivery_states import MonitorEvidence, Outcome, UnrecordedMonitor
from agent_comms.harness_install import HarnessSession
from agent_comms.message_reference import MessageReference
from agent_comms.typed_table import Column, ForeignKey, FrozenColumn, ImmutableRows, Index, NoDelete

if TYPE_CHECKING:
    from agent_comms.assignment_store import AssignmentStore


@dataclass(frozen=True, slots=True)
class HarnessDelivery(NoDelete, CoordinatorRow, declared_name="harness_deliveries"):
    """One original attempt to put a bus message into a recipient's harness session."""

    bridge: Annotated[type[HarnessSession], FrozenColumn(primary_key=True)]
    recipient_lookup: Annotated[ThreadLookup, FrozenColumn(primary_key=True)]
    message_id: Annotated[MessageId, FrozenColumn(primary_key=True)]
    seq: Annotated[int, FrozenColumn(check="seq > 0")]
    outcome: Annotated[type[Outcome], Column(index=True)]
    monitor: MonitorEvidence = field(default_factory=UnrecordedMonitor)

    unique = (("bridge", "recipient_lookup", "seq"),)
    indexes = (Index(("recipient_lookup", "seq", "message_id"), unique=False, where=None),)
    """The inbox asks per delivered message whether any bridge attempted it for this recipient."""

    @classmethod
    def references(cls):
        return (ForeignKey(columns=("recipient_lookup",), target=Participants,
                           target_columns=("participant_lookup",)),)

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def key(self) -> dict[str, object]:
        return {"bridge": self.bridge, "recipient_lookup": self.recipient_lookup,
                "message_id": self.message_id}

    def stored(self, db: sqlite3.Connection) -> tuple[Self, ...]:
        """This attempt as stored; one stored at another sequence is another message."""
        rows = type(self).select(db, **self.key())
        if rows and rows[0].seq != self.seq:
            raise IdentityConflict(
                f"message {self.message_id} was attempted at {rows[0].seq}, not {self.seq}")
        return tuple(rows)

    def write(self, db: sqlite3.Connection) -> None:
        self.stored(db)
        self.upsert(db)

    def first_write(self, db: sqlite3.Connection) -> bool:
        """Store a new attempt; False when this message was already attempted."""
        if self.stored(db):
            return False
        self.insert(db)
        return True

    def begin(self, assignments: AssignmentStore, db: sqlite3.Connection) -> bool:
        """The claim leaves the native queue in the same write that stores the attempt, so no
        crash lets a later native owner replay it. False when it was already attempted."""
        assignments.settle_harness_inputs(
            db, self.recipient_lookup, (self.reference,), lambda state: state.harness_handoff())
        return self.first_write(db)

    def record(self, assignments: AssignmentStore, db: sqlite3.Connection) -> None:
        """The attempt's outcome and the claim steps it declares commit together."""
        assignments.settle_harness_inputs(
            db, self.recipient_lookup, (self.reference,), self.outcome.claim_steps)
        self.write(db)

    @classmethod
    def latest(cls, db: sqlite3.Connection, bridge: type[HarnessSession],
               recipient_lookup: ThreadLookup) -> tuple[MessageReference, ...]:
        """The newest attempt of this bridge for this recipient, uncertain emissions included."""
        return tuple(row.reference for row in cls.select(
            db, bridge=bridge, recipient_lookup=recipient_lookup,
            order_by=("seq",), descending=True, limit=1))


@dataclass(frozen=True, slots=True)
class HarnessObservedPart(ImmutableRows, CoordinatorRow, declared_name="harness_observed_parts"):
    """One part of an attempted message as the harness transcript showed it."""

    bridge: Annotated[type[HarnessSession], FrozenColumn(primary_key=True)]
    recipient_lookup: Annotated[ThreadLookup, FrozenColumn(primary_key=True)]
    message_id: Annotated[MessageId, FrozenColumn(primary_key=True)]
    number: Annotated[int, FrozenColumn(primary_key=True, check="number > 0")]
    sender: str
    body: str
    record_id: str
    timestamp: float

    @classmethod
    def references(cls):
        return (ForeignKey(columns=("bridge", "recipient_lookup", "message_id"),
                           target=HarnessDelivery,
                           target_columns=("bridge", "recipient_lookup", "message_id")),)

    def key(self) -> dict[str, object]:
        return {"bridge": self.bridge, "recipient_lookup": self.recipient_lookup,
                "message_id": self.message_id, "number": self.number}

    @classmethod
    def of(cls, db: sqlite3.Connection, delivery: HarnessDelivery) -> tuple[Self, ...]:
        return tuple(cls.select(db, **delivery.key(), order_by=("number",)))
