"""Carry bridge journal delivery attempts into the coordinator, then retire the journal tables.

The bus owns each message, so an attempt keeps its seq and id, never the journal's payload copy;
the seq is read the way the retired journal declaration indexed it. Every journal must be bound to
the bus the files were taken from, or the carry refuses before any write. A Codex unit can hold two
journals of one bridge and recipient (its thread-named and its session-named binding): they carry
as one set, and one message stored twice must agree or the carry refuses. Bridges read attempts
only from the coordinator, so this runs before any bridge restarts on this runtime.
"""

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from agent_comms.cli_bridges.shared import Binding, BridgeDeliveryStore, BridgeStateTable
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, MessageId, TranscriptRecordId
from agent_comms.harness_delivery_states import EmittedOutcome, MonitorEvidence, Outcome, UnrecordedMonitor
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.typed_table import SQLiteTableName, TypedRow, WriterOwnedFile, WriterOwnedResource


@dataclass(frozen=True)
class KnownForgedObservation:
    """The one retired corrupt monitor record. On 2026-10-20 at 03:50 two claude-code attempts
    (seq 6251 and 6252) were hand-marked delivered with this record id, which no transcript record
    has. They carry what is true of them: emitted, nothing observed, so nothing replays them and
    nothing counts them as entered context. An input to this carry only, never a MonitorEvidence
    member or a stored record; the codec refuses every other malformed observation."""

    kind: Literal["observed_monitor"]
    record_id: Literal["commander-skip-old-heartbeats"]
    timestamp: float

    def evidence(self, recorded: type[Outcome]) -> tuple[type[Outcome], MonitorEvidence]:
        return EmittedOutcome, UnrecordedMonitor()


@dataclass(frozen=True)
class JournalAttempt(TypedRow):
    id: MessageId
    seq: int
    outcome: type[Outcome]
    monitor: str

    def evidence(self) -> tuple[type[Outcome], MonitorEvidence]:
        return FieldCodec.decode(MonitorEvidence | KnownForgedObservation,
                                 FieldCodec.parse(self.monitor)).evidence(self.outcome)

    query: ClassVar[str] = (
        "SELECT id, json_extract(payload, '$.seq') AS seq, outcome, monitor FROM delivery")


@dataclass(frozen=True)
class JournalPart(TypedRow):
    id: MessageId
    number: int
    sender: str
    body: str
    record_id: TranscriptRecordId
    timestamp: float

    query: ClassVar[str] = (
        "SELECT id, number, sender, body, record_id, timestamp FROM observed_part")


@dataclass(frozen=True)
class CarriedJournal:
    resource: WriterOwnedResource
    attempts: tuple[HarnessDelivery, ...]
    parts: tuple[HarnessObservedPart, ...]


class HarnessDeliveryAttempts(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)
    writer_owned = (BridgeDeliveryStore,)
    retired: ClassVar[frozenset[str]] = frozenset({"delivery", "observed_part"})

    def journals(self) -> tuple[CarriedJournal, ...]:
        """Every journal still holding attempt tables, read from a snapshot."""
        found = []
        for bridge, resource in BridgeDeliveryStore.journals(self.files):
            path = self.files.location(resource)
            self.require_confined(path)
            with closing(BridgeDeliveryStore.read_snapshot(path)) as db:
                if not {table.name for table in SQLiteTableName.stored(db)} & self.retired:
                    continue
                binding = Binding.require(db)
                if Path(binding.root) != self.files.source_root():
                    raise RelationViolationError(
                        f"{resource.path} is bound to {binding.root}, not {self.files.source_root()}; "
                        "its attempts are another bus's evidence")
                lookup = binding.owner.require_recorded().lookup
                found.append(CarriedJournal(
                    resource,
                    tuple(HarnessDelivery(bridge, lookup, row.id, row.seq, *row.evidence())
                          for row in JournalAttempt.read(db.execute(JournalAttempt.query))),
                    tuple(HarnessObservedPart(bridge, lookup, row.id, row.number, row.sender,
                                              row.body, row.record_id, row.timestamp)
                          for row in JournalPart.read(db.execute(JournalPart.query))),
                ))
        return tuple(found)

    @staticmethod
    def merged[Row: (HarnessDelivery, HarnessObservedPart)](
        rows: Iterable[tuple[Row, WriterOwnedResource]],
    ) -> tuple[Row, ...]:
        """One row per key across journals; one key stored with two values is refused."""
        kept: dict[tuple[object, ...], tuple[Row, WriterOwnedResource]] = {}
        for row, resource in rows:
            first, source = kept.setdefault(row.identity(), (row, resource))
            if first != row:
                raise RelationViolationError(
                    f"{source.path} and {resource.path} disagree on {first} and {row}; "
                    "refusing to choose")
        return tuple(row for row, _source in kept.values())

    @staticmethod
    def inserted[Row: (HarnessDelivery, HarnessObservedPart)](
        db: sqlite3.Connection, rows: tuple[Row, ...],
    ) -> None:
        """Insert each row once; a row already carried must equal it, or the carry refuses."""
        for row in rows:
            stored = row.stored(db)
            if not stored:
                row.insert(db)
            elif stored[0] != row:
                raise RelationViolationError(f"Coordinator already holds {stored[0]}, not {row}; "
                                             "refusing to choose")

    def carry(self, *, apply: bool) -> Carried:
        journals = self.journals()
        attempts = self.merged((row, journal.resource) for journal in journals for row in journal.attempts)
        parts = self.merged((row, journal.resource) for journal in journals for row in journal.parts)
        coordination = CoordinationStore.path_in(self.root)
        if apply and journals:
            with closing(CoordinationStore.writer(coordination)) as db, \
                    CoordinationStore.write_transaction(db):
                for table in (HarnessDelivery, HarnessObservedPart):
                    if not table.present(db):
                        for statement in table.ddl():
                            db.execute(statement)
                self.inserted(db, attempts)
                self.inserted(db, parts)
            for journal in journals:
                with closing(BridgeDeliveryStore.writer(self.files.location(journal.resource))) as db:
                    BridgeStateTable.install_schema(db, WriterOwnedFile())
                    BridgeStateTable.require_readable(db)
        return Carried(
            len(journals), detail={"attempts": len(attempts), "parts": len(parts)},
            rewritten=(coordination.relative_to(self.root),
                       *(journal.resource.path for journal in journals)) if apply and journals else (),
        )
