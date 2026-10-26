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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from .activity import ActivityState
from .child_process import ProcessIdentity
from .declared_family import DeclaredFamily
from .field_codec import RoomTicket
from .thread_identity import ThreadIncarnation
from .typed_table import Column, DurableStore, JsonStorage, StoreRows, TypedTable
from .watched import ChangeSignal, FileSource


@dataclass(frozen=True)
class RoomWork(DeclaredFamily, affix="RoomWork"):
    """One measured kind of work, with every owner sharing its request."""

    owners: tuple[ThreadIncarnation, ...]

    def estimate(self, measured: RoomNeed) -> RoomNeed:
        return measured.scaled(len(self.owners))

    def per_owner(self, measured: RoomNeed) -> RoomNeed:
        return measured.divided(len(self.owners))


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

    def scaled(self, count: int) -> RoomNeed:
        return RoomNeed(self.memory * count, self.disk * count)

    def divided(self, count: int) -> RoomNeed:
        return RoomNeed((self.memory + count - 1) // count, (self.disk + count - 1) // count)

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
    process: Annotated[ProcessIdentity, Column(storage=JsonStorage)]
    work: Annotated[tuple[RoomWork, ...], Column(storage=JsonStorage)]
    phase: Annotated[RoomPhase, Column(storage=JsonStorage)]
    at: float


@dataclass(frozen=True)
class RoomLedger(DurableStore):
    root: Path
    filename = "work_room.sqlite3"
    tables = RoomLedgerRows

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
                    WorkPeak.delete(db, work=type(work))
                    WorkPeak(type(work), work.per_owner(peak)).insert(db)
            RoomClaim.delete(db, ticket=ticket)

    def claims(self, root, threads):
        wanted = {thread.incarnation for thread in threads}
        with self.snapshot() as db:
            claims = RoomClaim.select(db, root=str(root))
        # A waiting sibling call must stay visible beside a running one.
        return {owner.name: claim
                for claim in sorted(claims, key=lambda row: row.phase.waiting)
                for work in claim.work for owner in work.owners
                if owner in wanted and claim.process.alive()}


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
            peaks = tuple(WorkPeak.one(db, work=type(work)) for work in claim.work)
            # An unmeasured run calibrates alone. It reserves the observed spare
            # room, never a typed-in estimate and never zero alongside another run.
            if any(peak is None for peak in peaks):
                if held != RoomNeed() or not self.room_floor().fits(free):
                    return False
                need = spare
            else:
                need = RoomNeed()
                for work, peak in zip(claim.work, peaks, strict=True):
                    need += work.estimate(peak.need)
                if not (need + self.room_floor() + held).fits(free):
                    return False
            RoomClaim.update(db, where='{ticket}=?', parameters=(ticket,),
                             phase=HoldingRoomPhase(need))
            return True

    async def wait_for_room(self, work: tuple[RoomWork, ...], root, *, process=None):
        """Wait without a registry/wire lock, atomically reserve, and measure."""
        process = process or ProcessIdentity.capture(os.getpid())
        ledger = self.room_ledger
        await asyncio.to_thread(ledger.root.mkdir, parents=True, exist_ok=True)
        ticket = RoomTicket.mint()
        claim = RoomClaim(ticket, str(root), process, work, WaitingRoomPhase(), time.time())
        admitted = False
        try:
            def enqueue():
                with ledger.writing() as db:
                    claim.insert(db)
            await asyncio.to_thread(enqueue)
            async with ChangeSignal.opened(*(
                FileSource(path) for path in ledger.source_paths(ledger.root)
            )) as changes:
                while True:
                    changes.prepare()
                    if await asyncio.to_thread(self._admit, ticket):
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
