"""Store files: declaration and persistence owners."""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ClassVar, TYPE_CHECKING

from .errors import RelationViolationError
from .private_path import PrivateFileRole

if TYPE_CHECKING:
    from .private_bus_checkpoint import CertifiedSourceRead
    from .wire_log import WireLog
    from .wire_metadata import WireMetadata
    from .child_process import Platform


@dataclass(slots=True)
class StoreLockContention:
    """One operation's borrowed wait resource, spent only at physical locks.

    Source reading, decoding and consumer work cannot spend this resource.
    It grants no owner, input, proof or replay permission.
    """

    remaining: float

    def waits(self, descriptor: int, platform: Platform, *, shared: bool) -> Iterator[float]:
        """One physical acquisition algorithm; drivers own sync/async waiting."""
        while True:
            begun = time.monotonic()
            try:
                platform.try_store_lock(descriptor, shared=shared)
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

    def acquire(self, descriptor: int, platform: Platform, *, shared: bool) -> None:
        for delay in self.waits(descriptor, platform, shared=shared):
            time.sleep(delay)

    async def acquire_async(self, descriptor: int, platform: Platform, *, shared: bool) -> None:
        for delay in self.waits(descriptor, platform, shared=shared):
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
        with log.verify_before_read_unlocked() as refreshed:
            if refreshed is None:
                raise RelationViolationError("Store has no certified original wire source.")
            refreshed.require_marker(metadata)
            yield refreshed


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
def _held_store_source(store_path, lock_file, platform, max_bus_bytes, *, shared=False):
    """One durability guard after physical custody, with exact release on refusal."""
    try:
        if max_bus_bytes is not None:
            if type(max_bus_bytes) is not int or max_bus_bytes < 0:
                raise ValueError("bus read cap must be a nonnegative integer")
            if store_path.exists():
                bus_info = store_path.lstat()
                if not stat.S_ISREG(bus_info.st_mode) or bus_info.st_size > max_bus_bytes:
                    raise RelationViolationError("Bus exceeds bounded read budget.")
        from .wire_log import WireLog

        with WireLog(store_path).verify_before_read_unlocked(shared=shared) as source:
            yield StoreLock(lock_file.fileno(), source)
    finally:
        platform.release_store_lock(lock_file.fileno())


@contextmanager
def _store_lock(
    store_path: Path,
    *,
    blocking: bool = True,
    max_bus_bytes: int | None = None,
    shared: bool = False,
    contention: StoreLockContention | None = None,
) -> Iterator[StoreLock]:
    """Canonical synchronous acquisition; inherited POSIX custody ends at last close."""
    from .child_process import Platform

    platform = Platform.current()
    with _store_lock_file(store_path) as lock_file:
        if contention is None:
            platform.acquire_store_lock(lock_file.fileno(), shared=shared, blocking=blocking)
        else:
            contention.acquire(lock_file.fileno(), platform, shared=shared)
        with _held_store_source(store_path, lock_file, platform, max_bus_bytes,
                                shared=shared) as lock:
            yield lock


@contextmanager
def _store_read_lock(
    store_path: Path,
    *,
    blocking: bool = True,
    contention: StoreLockContention | None = None,
) -> Iterator[StoreLock]:
    """Share source custody, escalating only when its checkpoint needs repair."""
    from .private_bus_checkpoint import CheckpointNeedsRepair

    with ExitStack() as resources:
        try:
            lock = resources.enter_context(
                _store_lock(
                    store_path, blocking=blocking, shared=True, contention=contention
                )
            )
        except CheckpointNeedsRepair:
            lock = resources.enter_context(
                _store_lock(store_path, blocking=blocking, contention=contention)
            )
        yield lock


@asynccontextmanager
async def _async_store_lock(
    store_path: Path,
    *,
    blocking: bool = True,
    max_bus_bytes: int | None = None,
    shared: bool = False,
    contention: StoreLockContention | None = None,
):
    """Borrow the same physical resource without blocking its owner event loop.

    Cancellation during acquisition closes only the unacquired descriptor. The
    original durability guard runs only after custody is acquired; no SQLite
    connection or consumer is transferred to another thread.
    """
    from .child_process import Platform

    platform = Platform.current()
    wait = contention if contention is not None else StoreLockContention(math.inf if blocking else 0)
    with _store_lock_file(store_path) as lock_file:
        await wait.acquire_async(lock_file.fileno(), platform, shared=shared)
        with _held_store_source(store_path, lock_file, platform, max_bus_bytes,
                                shared=shared) as lock:
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
