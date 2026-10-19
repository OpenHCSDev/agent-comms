"""Measured work and its machine-wide room, owned by StorageRule.

The ledger stores only peaks and live claims. Free room always comes from the OS.
Waiting rows are also the status source; no activity event mirrors their state.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from .activity import ActivityState
from .child_process import ProcessIdentity
from .thread_identity import ThreadIncarnation
from .declared_family import DeclaredFamily
from .typed_table import Column, JsonStorage, TypedStore, TypedTable
from .wire_watch import ModifiedWatchSource, WireWatch


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


@dataclass(frozen=True)
class WorkPeak(TypedTable):
    work: type[RoomWork] = field(metadata={"sql": Column(primary_key=True)})
    need: RoomNeed = field(metadata={"sql": Column(storage=JsonStorage)})


@dataclass(frozen=True)
class RoomClaim(TypedTable):
    ticket: str = field(metadata={"sql": Column(primary_key=True)})
    root: str
    unit: ThreadIncarnation = field(metadata={"sql": Column(storage=JsonStorage)})
    process: ProcessIdentity = field(metadata={"sql": Column(storage=JsonStorage)})
    work: tuple[type[RoomWork], ...] = field(metadata={"sql": Column(storage=JsonStorage)})
    phase: RoomPhase = field(metadata={"sql": Column(storage=JsonStorage)})
    at: float


@dataclass(frozen=True)
class RoomLedger(TypedStore):
    root: Path
    filename = "work_room.sqlite3"
    rows = (WorkPeak, RoomClaim)

    def source_paths(self):
        # SQLite attachment can create/remove WAL files without changing state.
        # Publish only committed row changes, never wake on reader attachment.
        return (self.path.with_suffix('.changed'),)

    @contextmanager
    def writing(self):
        changed = False
        with super().writing() as db:
            before = db.total_changes
            yield db
            changed = db.total_changes != before
        if changed:
            self.source_paths()[0].touch()

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
        if not self.process.alive():
            return 0
        tree = {self.process.pid}
        rows = {}
        for directory in self.rule.proc.glob('[0-9]*'):
            try:
                values = {line.partition(':')[0]: line.partition(':')[2].split()
                          for line in (directory / 'status').read_text().splitlines()}
                rows[int(directory.name)] = (int(values['PPid'][0]),
                    max(int(values.get('VmRSS', ['0'])[0]),
                        int(values.get('VmHWM', ['0'])[0])) * 1024)
            except (FileNotFoundError, ProcessLookupError):
                continue
        pending = set(rows) - tree
        while children := {pid for pid in pending if rows[pid][0] in tree}:
            tree.update(children)
            pending.difference_update(children)
        return sum(rows[pid][1] for pid in tree if pid in rows)

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


@dataclass
class RoomReservation:
    rule: RoomAllocation
    ticket: str
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

    @property
    def room_ledger(self):
        return RoomLedger(self.volume / '.cache' / 'agent-comms')

    def free_room(self):
        return RoomNeed(self.memory_available_bytes(), int(self.free_gib() * 2**30))

    def room_floor(self):
        return RoomNeed(int(self.memory_gib * 2**30), int(self.floor_gib * 2**30))

    def _admit(self, ticket):
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
            held = RoomNeed()
            for other in RoomClaim.select(db):
                held += other.phase.held
            spare = free.remaining(self.room_floor() + held)
            peaks = tuple(WorkPeak.one(db, work=work) for work in claim.work)
            # An unmeasured run calibrates alone. It reserves the observed spare
            # room, never a typed-in estimate and never zero alongside another run.
            if any(peak is None for peak in peaks):
                if held != RoomNeed() or not self.room_floor().fits(free):
                    return False
                need = spare
            else:
                need = RoomNeed()
                for peak in peaks:
                    need += peak.need
                if not (need + self.room_floor() + held).fits(free):
                    return False
            RoomClaim.update(db, where='{ticket}=?', parameters=(ticket,),
                             phase=HoldingRoomPhase(need))
            return True

    async def wait_for_room(self, work, thread, root, *, process=None):
        """Wait without a registry/wire lock, atomically reserve, and measure."""
        process = process or ProcessIdentity.capture(os.getpid())
        ledger = self.room_ledger
        await asyncio.to_thread(ledger.root.mkdir, parents=True, exist_ok=True)
        ticket = uuid4().hex
        claim = RoomClaim(ticket, str(root), thread.incarnation,
                          process, work, WaitingRoomPhase(), time.time())
        admitted = False
        try:
            def enqueue():
                with ledger.writing() as db:
                    claim.insert(db)
            await asyncio.to_thread(enqueue)
            source = ModifiedWatchSource(ledger.root, frozenset(
                os.fsencode(path.name) for path in ledger.source_paths()))
            async for watcher in WireWatch.observations(source):
                if await asyncio.to_thread(self._admit, ticket):
                    measurement = WorkMeasurement(self, process)
                    admitted = True
                    return RoomReservation(self, ticket, measurement)
                # Real release notifications race safely with prepare/admit. A
                # timeout refreshes external OS changes, not another denial.
                try:
                    async with asyncio.timeout(self.room_refresh_seconds):
                        await watcher.wait()
                except TimeoutError:
                    watcher.notify()
        finally:
            if not admitted:
                await asyncio.to_thread(ledger.release, ticket)

    @contextmanager
    def room_for_start(self, thread, root):
        if thread.process_alive:
            yield
            return
        self.require_allocation(thread)
        reservation = asyncio.run(self.wait_for_room((UnitStartRoomWork,), thread, root))
        try:
            yield
        except BaseException:
            reservation.finish(completed=False)
            raise
        else:
            reservation.finish()
