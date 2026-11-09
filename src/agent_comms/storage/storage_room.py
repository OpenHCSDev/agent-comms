"""Measured work and its machine-wide room, owned by StorageRule.

The ledger stores only peaks and live claims. Free room always comes from the OS.
Waiting rows are also the status source; no activity event mirrors their state.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, ClassVar

from agent_comms.execution.child_process import ProcessIdentity, ResidentMemory
from agent_comms.messaging.watched import ChangeSignal, FileSource
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import RoomTicket
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import PublishedProperty
from agent_comms.storage.store_files import StoreContention
from agent_comms.storage.typed_table import (
    Column,
    DurableTables,
    JsonStorage,
    RuntimeWriter,
    StoreRows,
    TypedStore,
    TypedTable,
    WriterOwnedFile,
)
from agent_comms.transport.tool_denial import StorageDenied


class RoomWork(DeclaredFamily, affix="RoomWork"):
    """Each declaration owns a separately measured kind of work."""


class BuildRoomWork(RoomWork):
    pass


class TestGateRoomWork(RoomWork):
    pass


class UnitStartRoomWork(RoomWork):
    pass


class AllocationRoomWork(RoomWork):
    """Other allocating operations, not mislabeled as builds or tests."""


@dataclass(frozen=True)
class RoomNeed:
    memory: int = 0
    disk: int = 0

    def __add__(self, other):
        return RoomNeed(self.memory + other.memory, self.disk + other.disk)

    def remaining(self, other):
        return RoomNeed(max(0, self.memory - other.memory), max(0, self.disk - other.disk))

    def fits(self, free):
        return self.memory <= free.memory and self.disk <= free.disk

    def peak(self, other):
        return RoomNeed(max(self.memory, other.memory), max(self.disk, other.disk))


@dataclass(frozen=True)
class RoomPhase(DeclaredFamily, affix="RoomPhase"):
    waiting: ClassVar[bool] = False
    held: ClassVar[RoomNeed] = RoomNeed()

    def project(self, activity, work):
        return activity


class WaitingRoomPhase(RoomPhase):
    waiting = True

    def project(self, activity, work):
        return activity.with_status(ActivityState.WAITING_FOR_ROOM,
                                    ', '.join(kind.declared_name for kind in work))


@dataclass(frozen=True)
class HoldingRoomPhase(RoomPhase):
    held: RoomNeed


@dataclass
class RoomShortfall(Exception):  # raising sets __traceback__, so never frozen
    """Why one admission decision did not fit, in the numbers it compared."""

    need: RoomNeed
    floor: RoomNeed
    held: tuple[tuple[str, tuple[type[RoomWork], ...], RoomNeed], ...]
    free: RoomNeed

    def __str__(self) -> str:
        gib = lambda need: f"{need.memory / 2**30:.1f} GiB RAM, {need.disk / 2**30:.1f} GiB disk"
        holders = "; ".join(f"{unit} ({', '.join(kind.declared_name for kind in work)}): {gib(need)}"
                            for unit, work, need in self.held) or "nothing"
        return (f"needs {gib(self.need)} above the {gib(self.floor)} floor; free is {gib(self.free)};"
                f" other runs hold: {holders}")


class RoomLedgerRows(StoreRows):
    """The tables of RoomLedger; each row declares itself one."""


@dataclass(frozen=True)
class WorkPeak(RoomLedgerRows, TypedTable):
    work: Annotated[type[RoomWork], Column(primary_key=True)]
    need: Annotated[RoomNeed, Column(storage=JsonStorage)]


@dataclass(frozen=True)
class RoomClaim(RoomLedgerRows, TypedTable):
    ticket: Annotated[RoomTicket, Column(primary_key=True)]
    root: str
    unit: Annotated[ThreadIncarnation, Column(storage=JsonStorage)]
    process: Annotated[ProcessIdentity, Column(storage=JsonStorage)]
    work: Annotated[tuple[type[RoomWork], ...], Column(storage=JsonStorage)]
    phase: Annotated[RoomPhase, Column(storage=JsonStorage)]
    at: float


@dataclass(frozen=True)
class RoomLedger(TypedStore, DurableTables, RuntimeWriter):
    root: Path
    filename = "work_room.sqlite3"
    tables = RoomLedgerRows

    @classmethod
    def _open_reader(
        cls, path: Path, contention: StoreContention, *, check_same_thread: bool = True,
    ) -> Connection:
        """Carry this host-owned file before its first snapshot; then prohibit writes."""
        connection = cls.writer(
            path.resolve().as_uri() + "?mode=rw", contention,
            isolation_level=None, uri=True, check_same_thread=check_same_thread,
        )
        try:
            cls.install_schema(connection, WriterOwnedFile())
            return cls._configure_reader(connection)
        except BaseException:
            connection.close()
            raise

    @classmethod
    def schema_change(cls, path: Path) -> WriterOwnedFile:
        """The host-wide ledger is carried under its writer lock, outside any one bus."""
        return WriterOwnedFile()

    def dead_claims(self):
        # Dead/replaced processes cannot hold room, but are never replayed.
        with self.snapshot() as db:
            claims = RoomClaim.select(db)
        return tuple(claim.ticket for claim in claims if not claim.process.alive())

    def release(self, ticket, peak=None):
        with self.writing() as db:
            claim = RoomClaim.one(db, ticket=ticket)
            if claim is not None and peak is not None:
                for work in claim.work:
                    WorkPeak.delete(db, work=work)
                    WorkPeak(work, peak).insert(db)
            RoomClaim.delete(db, ticket=ticket)

    def claims(self, root, threads):
        wanted = {thread.incarnation for thread in threads}
        with self.snapshot() as db:
            claims = RoomClaim.select(db, root=str(root))
        # A waiting sibling call must stay visible beside a running one.
        return {claim.unit.name: claim for claim in sorted(claims, key=lambda row: row.phase.waiting)
                if claim.unit in wanted and claim.process.alive()}


class WorkMeasurement:
    """One run's sampled process-tree RSS/HWM and peak free-disk delta."""
    sample_seconds = 0.05

    def __init__(self, rule, process):
        self.rule, self.process = rule, process
        self.disk_before = rule.free_room().disk
        self.peak = RoomNeed()
        self.done = threading.Event()
        self.worker = threading.Thread(target=self.observe, daemon=True)
        self.worker.start()

    def resident(self):
        return sum(max(row.resident, row.peak)
                   for row in ResidentMemory.tree(self.process, self.rule.proc).values())

    def observe(self):
        while True:
            self.peak = self.peak.peak(RoomNeed(self.resident(),
                max(0, self.disk_before - self.rule.free_room().disk)))
            if self.done.wait(self.sample_seconds):
                break

    def finish(self):
        self.done.set()
        self.worker.join()
        return self.peak


class RoomReservation(ABC):
    """One tool's room lifetime, including work that needs no room."""

    @abstractmethod
    def finish(self, *, completed=True): ...

    @abstractmethod
    def abandon(self): ...

    @asynccontextmanager
    async def checking(self):
        """Failed authorization cannot keep room for a tool that never ran."""
        try:
            yield self
        except BaseException:
            await asyncio.to_thread(self.finish, completed=False)
            raise


class NoRoomNeeded(RoomReservation):
    def finish(self, *, completed=True):
        pass

    def abandon(self):
        pass


@dataclass
class MeasuredRoomReservation(RoomReservation):
    rule: RoomAllocation
    ticket: RoomTicket
    measurement: WorkMeasurement

    def finish(self, *, completed=True):
        peak = self.measurement.finish()
        self.rule.room_ledger.release(self.ticket, peak if completed else None)

    def abandon(self):
        """Stop measuring UNKNOWN work; its process still holds the recorded claim."""
        self.measurement.finish()
        ledger = self.rule.room_ledger
        dead = ledger.dead_claims()
        with ledger.writing() as db:
            for ticket in dead:
                RoomClaim.delete(db, ticket=ticket)


class RoomAllocation:
    """StorageRule's admission and measurement behavior, shared by all callers."""
    work_kinds: ClassVar[type[RoomWork]] = RoomWork
    # A release wakes through inotify. The timed OS refresh also observes room
    # freed by jobs outside Core, which cannot notify this ledger.
    room_refresh_seconds = 1.0
    room_wait_seconds = 120.0
    """A run waits this long for room, then is refused with the numbers, never parked: on
    2026-11-02 two teammates' turns sat an hour on test_gate, unable to receive anything."""

    @PublishedProperty
    def room_ledger(self) -> RoomLedger:
        return RoomLedger(self.volume / '.cache' / 'agent-comms')

    def free_room(self):
        return RoomNeed(self.memory_available_bytes(), int(self.free_gib() * 2**30))

    def room_floor(self):
        return RoomNeed(int(self.memory_gib * 2**30), int(self.floor_gib * 2**30))

    def _admit(self, ticket):
        """Reserve room for this claim, or raise RoomShortfall with the numbers that did not fit.
        A recorded peak keeps concurrent runs from overcommitting; a run alone whose peak does
        not fit takes the spare room above the floor, since with nothing else holding room no
        wait frees more of it (2026-11-02: a small test waited an hour on an earlier run's peak)."""
        ledger = self.room_ledger
        # OS reads and /proc traversal never hold the writer transaction.
        free = self.free_room()
        dead = ledger.dead_claims()
        with ledger.writing() as db:
            for stale in dead:
                RoomClaim.delete(db, ticket=stale)
            claim = RoomClaim.one(db, ticket=ticket)
            if claim is None:
                raise RuntimeError('Room waiter disappeared')
            holders = tuple(other for other in RoomClaim.select(db) if other.phase.held != RoomNeed())
            held = RoomNeed()
            for other in holders:
                held += other.phase.held
            spare = free.remaining(self.room_floor() + held)
            peaks = tuple(WorkPeak.one(db, work=work) for work in claim.work)
            need = RoomNeed()
            for peak in peaks:
                need += peak.need if peak is not None else RoomNeed()
            # A measured run reserves its recorded peaks when they fit. Otherwise a run alone
            # reserves the observed spare room (an unmeasured run calibrates the same way); beside
            # other runs it waits, never reserving zero alongside another.
            if any(peak is None for peak in peaks) or not (need + self.room_floor() + held).fits(free):
                if holders or not self.room_floor().fits(free):
                    raise RoomShortfall(need, self.room_floor(), tuple(
                        (other.unit.name, other.work, other.phase.held) for other in holders), free)
                need = spare
            RoomClaim.update(db, where='{ticket}=?', parameters=(ticket,),
                             phase=HoldingRoomPhase(need))

    async def wait_for_room(self, work, thread, root, *, process=None):
        """Wait without a registry/wire lock, atomically reserve, and measure."""
        process = process or ProcessIdentity.capture(os.getpid())
        ledger = self.room_ledger
        await asyncio.to_thread(ledger.root.mkdir, parents=True, exist_ok=True)
        ticket = RoomTicket.mint()
        claim = RoomClaim(ticket, str(root), thread.incarnation,
                          process, work, WaitingRoomPhase(), time.time())
        admitted = False
        try:
            def enqueue():
                with ledger.writing() as db:
                    claim.insert(db)
            await asyncio.to_thread(enqueue)
            async with ChangeSignal.opened(*(
                FileSource(path) for path in ledger.source_paths(ledger.root)
            )) as changes:
                deadline = time.monotonic() + self.room_wait_seconds
                while True:
                    changes.prepare()
                    try:
                        await asyncio.to_thread(self._admit, ticket)
                    except RoomShortfall as shortfall:
                        if time.monotonic() >= deadline:
                            raise StorageDenied(
                                f"StorageRule: no room for {', '.join(kind.declared_name for kind in work)}"
                                f" after {self.room_wait_seconds:.0f}s: {shortfall}. Retry when a"
                                " holder finishes, or run a smaller selection.") from None
                    else:
                        measurement = WorkMeasurement(self, process)
                        admitted = True
                        return MeasuredRoomReservation(self, ticket, measurement)
                    # OS free space can change without any Core ledger write.
                    try:
                        async with asyncio.timeout(self.room_refresh_seconds):
                            await changes.wait()
                    except TimeoutError:
                        pass
        finally:
            if not admitted:
                await asyncio.to_thread(ledger.release, ticket)

    @contextmanager
    def room_for_start(self, thread, root):
        if thread.process_alive:
            yield
            return
        self.require_floor()
        reservation = asyncio.run(self.wait_for_room((UnitStartRoomWork,), thread, root))
        try:
            yield
        except BaseException:
            reservation.finish(completed=False)
            raise
        else:
            reservation.finish()
