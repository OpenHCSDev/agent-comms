"""An append-only, capability-declared SQLite bus; no existing readers are wired here."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from agent_comms.bus_publication import Delivered
from agent_comms.bus_tables import (
    Addressed,
    BusRow,
    BusTables,
    KeyedPublication,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import MessageNotificationError, RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import DurableFile, RegistryFile
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.messages import BusRecord, ClaimMessage
from agent_comms.typed_table import SQLiteTableName, StoreNotificationError, StoreWriter


class PublicRecord(BusRecord):
    pass


class ClaimRecord(PublicRecord, ClaimMessage):
    pass


class PrivateRecord(Delivered):
    pass


class KeyedPrivateRecord(PrivateRecord, KeyedPublication):
    pass


class Bus(StoreWriter, DurableFile):
    """One SQLite transaction owns the sequence and all of a record's fields."""

    filename = "bus.sqlite3"

    def __init__(self, path: Path):
        super().__init__(path)

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(CoordinationStore.writer(self.path, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with self.transaction(db):
                if not SQLiteTableName.stored(db):
                    BusTables.create_schema(db)
                BusTables.require_schema(db)
                yield db

    @contextmanager
    def observing(self) -> Iterator[sqlite3.Connection]:
        """Borrow one existing read-only snapshot, including for an archived bus."""
        with CoordinationStore.observing(self.path) as db:
            BusTables.require_schema(db)
            yield db

    def revision(self) -> int:
        with self.observing() as db:
            return BusRow.last_seq(db)

    def backup(self, destination: Path) -> None:
        """Copy a consistent SQLite snapshot, never replace an existing destination."""
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.touch(mode=0o600, exist_ok=False)
        try:
            with self.observing() as source, closing(
                CoordinationStore.writer(destination, isolation_level=None)
            ) as target:
                source.backup(target)
        except BaseException:
            destination.unlink()
            raise

    def append(self, build: Callable[[int], BusRecord]) -> BusRecord:
        with ExitStack() as writes:
            db = writes.enter_context(self.writing())
            MaintenanceBarrier(RegistryFile.path_in(self.path.parent)).assert_open_unlocked()
            seq = BusRow.next_seq(db)
            record = build(seq)
            committed = self._record(db, BusRow.require(db, seq=record.append_to(db, seq)))
            try:
                writes.close()
            except StoreNotificationError as error:
                raise MessageNotificationError(committed) from error
            return committed

    @staticmethod
    def _record(db: sqlite3.Connection, header: BusRow) -> BusRecord:
        member = header.kind
        values = {}
        for table in member.tables():
            rows = table.select(db, seq=header.seq)
            if not rows:
                raise RelationViolationError(
                    f"Bus row {header.seq} lacks {table.declared_name}")
            values.update(FieldCodec.encode_fields(rows[0]))
        return FieldCodec.decode_record(member, values)

    def lookup(self, seq: int) -> BusRecord:
        with self.observing() as db:
            headers = BusRow.select(db, seq=seq)
            if not headers:
                raise RelationViolationError(f"Unknown bus sequence: {seq}")
            return self._record(db, headers[0])

    def page(self, before: int, limit: int) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            return tuple(self._record(db, row) for row in BusRow.select(
                db, where="{seq} < ?", parameters=(before,),
                order_by=("seq",), descending=True, limit=limit))

    def since(self, after: int, limit: int) -> tuple[BusRecord, ...]:
        """One ascending keyset page after the last scanned bus sequence."""
        with CoordinationStore.observing(self.path) as db:
            return tuple(self._record(db, row) for row in BusRow.select(
                db, where="{seq} > ?", parameters=(after,),
                order_by=("seq",), limit=limit))

    def inbox(self, target: str, after: int, limit: int) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            addressed = Addressed.table.select(
                db, where="{seq} > ?", parameters=(after,), order_by=("seq",), limit=limit,
                target=target)
            return tuple(self._record(db, header) for row in addressed
                         for header in BusRow.select(db, seq=row.seq))
