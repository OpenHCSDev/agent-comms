"""Harness delivery attempts: what a bridge did with one bus message for one recipient.

The bus owns the message, so an attempt keeps only its reference. The attempt commits in
the same coordinator write as the claim step it causes, and the inbox reads consumption
from these rows beside the claims, from one store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import batched
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.coordination_tables.participants import Participants
from agent_comms.field_codec import MessageId, ThreadLookup, TranscriptRecordId
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

    key_batch: ClassVar[int] = 32766 // 3
    unique = (("bridge", "recipient_lookup", "seq"),)
    indexes = (Index(("recipient_lookup", "seq", "message_id"), unique=False, where=None),)
    """The inbox asks per delivered message whether any bridge attempted it for this recipient."""

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(columns=("recipient_lookup",), target=Participants,
                           target_columns=("participant_lookup",)),)

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def identity(self) -> tuple[type[HarnessSession], ThreadLookup, MessageId]:
        return self.bridge, self.recipient_lookup, self.message_id

    def stored(self, db: sqlite3.Connection) -> tuple[Self, ...]:
        """This attempt as stored; one stored at another sequence is another message."""
        rows = type(self).select(db, bridge=self.bridge, recipient_lookup=self.recipient_lookup,
                                 message_id=self.message_id)
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

    def apply(self, change: Callable[[HarnessDelivery], HarnessDelivery],
              assignments: AssignmentStore, db: sqlite3.Connection) -> HarnessDelivery:
        """Change the attempt as stored inside this write, never the copy a caller read
        earlier: a confirmation landing between that read and this write stays recorded."""
        stored, = self.stored(db)
        return stored.outcome.settle(change(stored), assignments, db)

    @classmethod
    def for_recipients(cls, db: sqlite3.Connection,
                       keys: Sequence[tuple[ThreadLookup, MessageReference]]) -> tuple[Self, ...]:
        """Every bridge's attempt on exactly these (recipient, original) keys, each probed
        through the recipient/seq/message index; no read walks the attempt history. Keys go
        in batches that keep three parameters each under SQLite's variable limit."""
        return tuple(sorted(
            (row for batch in batched(keys, cls.key_batch) for row in cls.select(
                db,
                where=("(recipient_lookup,seq,message_id) IN (VALUES "
                       + ",".join("(?,?,?)" for _ in batch) + ")"),
                parameters=tuple(value for lookup, reference in batch
                                 for value in (lookup, reference.seq, reference.message_id)),
            )),
            key=lambda row: (row.seq, row.recipient_lookup, row.bridge.declared_name),
        ))

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
    record_id: TranscriptRecordId
    timestamp: float

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(columns=("bridge", "recipient_lookup", "message_id"),
                           target=HarnessDelivery,
                           target_columns=("bridge", "recipient_lookup", "message_id")),)

    def identity(self) -> tuple[type[HarnessSession], ThreadLookup, MessageId, int]:
        return self.bridge, self.recipient_lookup, self.message_id, self.number

    def stored(self, db: sqlite3.Connection) -> tuple[Self, ...]:
        return tuple(type(self).select(db, bridge=self.bridge, recipient_lookup=self.recipient_lookup,
                                       message_id=self.message_id, number=self.number))

    @classmethod
    def of(cls, db: sqlite3.Connection, delivery: HarnessDelivery) -> tuple[Self, ...]:
        return tuple(cls.select(db, bridge=delivery.bridge, recipient_lookup=delivery.recipient_lookup,
                                message_id=delivery.message_id, order_by=("number",)))
