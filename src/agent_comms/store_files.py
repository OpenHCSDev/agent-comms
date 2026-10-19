"""Store files: declaration and persistence owners."""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ClassVar, TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .private_path import PrivateFileRole

with suppress(ModuleNotFoundError):  # Windows; Win32Platform never asks a mode for its flock.
    import fcntl

if TYPE_CHECKING:
    import sqlite3

    from .private_bus_checkpoint import (
        CertifiedSourceRead,
        CheckpointNeedsRepair,
        PrefixWitness,
        WritableCheckpoint,
    )
    from .wire_log import WireLog
    from .wire_metadata import WireMetadata
    from .child_process import Platform


class LockMode(ABC):
    """Whether a store lock lets other holders in, and what its holder may do."""

    @abstractmethod
    def flock(self) -> int:
        """This mode's POSIX flock operation."""

    @abstractmethod
    def open_checkpoint(self, checkpoint: WritableCheckpoint) -> sqlite3.Connection:
        """Open a writable bus checkpoint as this lock's holder may."""

    @abstractmethod
    def verify_checkpoint(self, checkpoint: WritableCheckpoint, bus: WireLog,
                          marker: WireMetadata, db: sqlite3.Connection,
                          stream: BinaryIO) -> PrefixWitness:
        """Verify a writable bus checkpoint as this lock's holder may."""

    @abstractmethod
    def require_writer(self) -> None:
        """Require the exclusive holder before rebuilding a replaced source."""

    @abstractmethod
    def after_repair_needed(self, error: CheckpointNeedsRepair) -> LockMode:
        """The mode that may repair what this holder could only name."""


class SharedLock(LockMode):
    """Concurrent readers; the checkpoint is only checked, never repaired."""

    def flock(self) -> int:
        return fcntl.LOCK_SH

    def open_checkpoint(self, checkpoint: WritableCheckpoint) -> sqlite3.Connection:
        return checkpoint.open_read_only()

    def verify_checkpoint(self, checkpoint: WritableCheckpoint, bus: WireLog,
                          marker: WireMetadata, db: sqlite3.Connection,
                          stream: BinaryIO) -> PrefixWitness:
        return checkpoint.check_final(bus, marker, db, stream)

    def require_writer(self) -> None:
        from .private_bus_checkpoint import CheckpointNeedsRepair
        raise CheckpointNeedsRepair("Replaced bus file needs its page index rebuilt.")

    def after_repair_needed(self, error: CheckpointNeedsRepair) -> LockMode:
        return ExclusiveLock()


class ExclusiveLock(LockMode):
    """One holder, which may repair the checkpoint it verifies."""

    def flock(self) -> int:
        return fcntl.LOCK_EX

    def open_checkpoint(self, checkpoint: WritableCheckpoint) -> sqlite3.Connection:
        return checkpoint.open_for_repair()

    def verify_checkpoint(self, checkpoint: WritableCheckpoint, bus: WireLog,
                          marker: WireMetadata, db: sqlite3.Connection,
                          stream: BinaryIO) -> PrefixWitness:
        return checkpoint.verify_with_repair(bus, marker, db, stream)

    def require_writer(self) -> None:
        pass

    def after_repair_needed(self, error: CheckpointNeedsRepair) -> LockMode:
        raise error


@dataclass(slots=True)
class StoreLockContention:
    """One operation's borrowed wait resource, spent only at physical locks.

    Source reading, decoding and consumer work cannot spend this resource.
    It grants no owner, input, proof or replay permission.
    """

    remaining: float

    def waits(self, descriptor: int, platform: Platform, *, mode: LockMode) -> Iterator[float]:
        """One physical acquisition algorithm; drivers own sync/async waiting."""
        while True:
            begun = time.monotonic()
            try:
                platform.try_store_lock(descriptor, mode=mode)
            except BlockingIOError:
                self.remaining -= time.monotonic() - begun
                if self.remaining <= 0:
                    raise
                begun = time.monotonic()
                try:
                    yield min(self.remaining, platform.store_lock_interval)
                finally:
                    self.remaining -= time.monotonic() - begun
            else:
                return

    def acquire(self, descriptor: int, platform: Platform, *, mode: LockMode) -> None:
        for delay in self.waits(descriptor, platform, mode=mode):
            time.sleep(delay)

    async def acquire_async(self, descriptor: int, platform: Platform, *, mode: LockMode) -> None:
        for delay in self.waits(descriptor, platform, mode=mode):
            await asyncio.sleep(delay)


@dataclass
class StoreLockRelease:
    """Actions owned by one outer physical-lock lifetime."""

    actions: ExitStack = field(default_factory=ExitStack)
    active: bool = True

    def require_active(self) -> None:
        if not self.active:
            raise RelationViolationError("Store lock release scope has ended.")

    def callback(self, operation, /, *args, **kwargs) -> None:
        self.require_active()
        self.actions.callback(operation, *args, **kwargs)

    def close(self) -> None:
        if self.active:
            # Callbacks may take fresh locks; every inherited context sees this
            # scope as ended before the first callback runs.
            self.active = False
            self.actions.close()


@dataclass(frozen=True)
class StoreLock:
    """One physical lock's descriptor and opened durability resource."""

    descriptor: int
    source: CertifiedSourceRead | None
    _releases: ClassVar[ContextVar[StoreLockRelease]] = ContextVar(
        "store_lock_release", default=StoreLockRelease(active=False)
    )

    @classmethod
    @contextmanager
    def open_source(cls, path: Path, mode: LockMode):
        """Generic file locks lend no bus data or recovery authority."""
        yield None

    @classmethod
    @contextmanager
    def ordered(cls, requests: Mapping[StoreLockOwner, LockMode], *, blocking=True, contention=None):
        """Acquire store owners by their declared rank, never their filenames."""
        with ExitStack() as resources:
            yield {owner: resources.enter_context(owner.locked(
                mode=requests[owner], blocking=blocking, contention=contention))
                for owner in sorted(requests, key=lambda owner: (owner.lock_rank, str(owner.path)))}

    @classmethod
    def current_release(cls) -> StoreLockRelease:
        scope = cls._releases.get()
        scope.require_active()
        return scope

    @classmethod
    def worker_context(cls) -> Context:
        """Carry only the active lock lifetime into joined work."""
        context = Context()
        scope = cls._releases.get()
        if scope.active:
            context.run(cls._releases.set, scope)
        return context

    @classmethod
    @contextmanager
    def releasing(cls):
        """Nested locks share the outer lock's post-release work.

        The scope object, not a context-specific token, owns retirement. Marking
        it inactive retires every inherited context before callbacks take fresh
        locks. A stale inherited value therefore starts an independent scope.
        """
        current = cls._releases.get()
        if current.active:
            yield current
            return
        scope = StoreLockRelease()
        cls._releases.set(scope)
        try:
            yield scope
        finally:
            scope.close()

    def certified_read(self) -> CertifiedSourceRead:
        if self.source is None:
            raise RelationViolationError("Store has no certified original wire source.")
        return self.source

    @contextmanager
    def current_source(
        self, log: WireLog, metadata: WireMetadata
    ) -> Iterator[CertifiedSourceRead]:
        """Lend the current certificate under this retained physical bus lock.

        A prior append may have advanced the immutable source originally lent
        to this lock. Refresh that view, never reconstruct claim state or scan
        an uncertified bus.
        """
        source = self.certified_read()
        if source.marker == metadata:
            source.require_open_prefix()
            yield source
            return
        with self.open_source(log.path, ExclusiveLock()) as refreshed:
            if refreshed is None:
                raise RelationViolationError("Store has no certified original wire source.")
            refreshed.require_marker(metadata)
            yield refreshed


class StoreOwner(DeclaredFamily):
    """Shared declaration membership for composable store capabilities."""

    __slots__ = ()


class StoreLockOwner(StoreOwner, declared_name=None):
    """The composition root orders owners; each rank derives from that order."""

    __slots__ = ()
    path: Path
    _lock_order: ClassVar[tuple[type[StoreLockOwner], ...]] = ()
    lock_type: ClassVar[type[StoreLock]] = StoreLock

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "lock_rank" in vars(cls):
            raise TypeError("Store lock ranks derive from the composition root")

    @classmethod
    def declare_lock_order(cls, *owners: type[StoreLockOwner]) -> None:
        if len(set(owners)) != len(owners):
            raise TypeError("A store owner occurs twice in the lock order")
        missing = set(cls.members_with(cls)) - set(owners)
        if missing:
            names = ", ".join(sorted(owner.__name__ for owner in missing))
            raise TypeError(f"Store owners missing from the lock order: {names}")
        StoreLockOwner._lock_order = owners

    @property
    def lock_rank(self) -> int:
        try:
            return StoreLockOwner._lock_order.index(type(self))
        except ValueError:
            raise TypeError(f"{type(self).__name__} is absent from the declared lock order") from None

    def locked(self, *, blocking: bool = True, max_bus_bytes: int | None = None,
               mode: LockMode = ExclusiveLock(), contention: StoreLockContention | None = None):
        return _store_lock(self.path, blocking=blocking, max_bus_bytes=max_bus_bytes,
                           mode=mode, contention=contention, lock_type=self.lock_type)


def store_lock_path(store_path: Path) -> Path:
    """The original named inode for one store's physical lock."""
    return store_path.with_name(f'.{store_path.name}.lock')


@contextmanager
def _store_lock_file(store_path: Path):
    """Own the original lock inode's descriptor before any acquisition.

    The lock file is never written, so it is opened read-only: closing a
    descriptor opened for writing is a close-write event that wakes every
    owner watching the store directory, once per lock taken.
    """
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_lock_path(store_path)
    with StoreLock.releasing(), os.fdopen(
        os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o666), "rb"
    ) as lock_file:
        yield lock_file


@contextmanager
def _held_store_source(store_path, lock_file, platform, max_bus_bytes, *, mode, lock_type):
    """Check durability after locking; release the lock if the check refuses access."""
    try:
        if max_bus_bytes is not None:
            if type(max_bus_bytes) is not int or max_bus_bytes < 0:
                raise ValueError("bus read cap must be a nonnegative integer")
            if store_path.exists():
                bus_info = store_path.lstat()
                if not stat.S_ISREG(bus_info.st_mode) or bus_info.st_size > max_bus_bytes:
                    raise RelationViolationError("Bus exceeds bounded read budget.")
        with lock_type.open_source(store_path, mode) as source:
            yield lock_type(lock_file.fileno(), source)
    finally:
        platform.release_store_lock(lock_file.fileno())


@contextmanager
def _store_lock(
    store_path: Path,
    *,
    blocking: bool = True,
    max_bus_bytes: int | None = None,
    mode: LockMode = ExclusiveLock(),
    contention: StoreLockContention | None = None,
    lock_type: type[StoreLock] = StoreLock,
) -> Iterator[StoreLock]:
    """Acquire a store lock synchronously; inherited POSIX locks end at last close."""
    from .child_process import Platform
    from .private_bus_checkpoint import CheckpointNeedsRepair

    platform = Platform.current()
    with _store_lock_file(store_path) as lock_file, ExitStack() as resources:
        def acquire(mode):
            if contention is None:
                platform.acquire_store_lock(lock_file.fileno(), mode=mode, blocking=blocking)
            else:
                contention.acquire(lock_file.fileno(), platform, mode=mode)
            return resources.enter_context(_held_store_source(
                store_path, lock_file, platform, max_bus_bytes, mode=mode, lock_type=lock_type))

        try:
            lock = acquire(mode)
        except CheckpointNeedsRepair as error:
            lock = acquire(mode.after_repair_needed(error))
        yield lock


@asynccontextmanager
async def _async_store_lock(
    store_path: Path,
    *,
    blocking: bool = True,
    max_bus_bytes: int | None = None,
    mode: LockMode = ExclusiveLock(),
    contention: StoreLockContention | None = None,
    lock_type: type[StoreLock] = StoreLock,
):
    """Borrow the same physical resource without blocking its owner event loop.

    Cancellation during acquisition closes only the unacquired descriptor. The
    durability check runs only after the lock is acquired; no SQLite
    connection or consumer is transferred to another thread.
    """
    from .child_process import Platform

    platform = Platform.current()
    wait = contention if contention is not None else StoreLockContention(math.inf if blocking else 0)
    with _store_lock_file(store_path) as lock_file:
        await wait.acquire_async(lock_file.fileno(), platform, mode=mode)
        with _held_store_source(store_path, lock_file, platform, max_bus_bytes,
                                mode=mode, lock_type=lock_type) as lock:
            yield lock


def _atomic_write_text(
    path: Path, text: str, *, fsync_parent: bool = False,
    mode: int = PrivateFileRole.permissions,
) -> None:
    """Replace a snapshot; private guarded writes also durably sync its name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.chmod(temporary_path, mode)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        if fsync_parent:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _repair_trailing_jsonl(path: Path) -> None:
    """Complete a valid unterminated record or quarantine a truncated one."""
    try:
        with path.open("rb") as stream:
            if not stream.seek(0, os.SEEK_END):
                return
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) == b"\n":
                return
            stream.seek(0)
            data = stream.read()
    except FileNotFoundError:
        return
    boundary = data.rfind(b"\n") + 1
    tail = data[boundary:]
    try:
        json.loads(tail)
    except (json.JSONDecodeError, UnicodeDecodeError):
        corrupt_path = path.with_name(f"{path.name}.corrupt")
        with open(corrupt_path, "ab") as corrupt:
            corrupt.write(tail + b"\n")
            corrupt.flush()
            os.fsync(corrupt.fileno())
        with open(path, "r+b") as output:
            output.truncate(boundary)
            output.flush()
            os.fsync(output.fileno())
    else:
        with open(path, "ab") as output:
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())


def _iter_jsonl_stream(
    records: BinaryIO, *, boundary: int | None = None, label: str = "wire"
) -> Iterator[tuple[Mapping, int]]:
    """One raw-record parser for locked logs and fixed opened-inode snapshots."""
    while boundary is None or records.tell() < boundary:
        raw_line = (
            records.readline() if boundary is None else records.readline(boundary - records.tell())
        )
        if not raw_line:
            break
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if not raw_line.endswith((b"\n", b"\r")):
                break
            raise
        if not isinstance(record, Mapping):
            if label == "wire snapshot":
                raise ValueError("Wire snapshot JSONL record must be an object.")
            raise ValueError(f"JSONL record in {label} must be an object.")
        yield record, len(raw_line)


def _iter_jsonl_records(path: Path) -> Iterator[tuple[Mapping, int]]:
    """Yield JSONL records and encoded sizes without materializing the log."""
    if not path.exists():
        return
    with open(path, "rb") as records:
        yield from _iter_jsonl_stream(records, label=str(path))


def _jsonl_records(path: Path) -> list[Mapping]:
    """Read complete JSONL records, tolerating only a truncated final record."""
    return [record for record, _ in _iter_jsonl_records(path)]


def _append_jsonl(path: Path, record: Mapping) -> None:
    """Append one durable record. Caller must hold the store lock."""
    _repair_trailing_jsonl(path)
    with open(path, "ab") as output:
        output.write(json.dumps(record).encode() + b"\n")
        output.flush()
        os.fsync(output.fileno())
