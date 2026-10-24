"""Store files: declaration and persistence owners."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import math
import os
import stat
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO, ClassVar

from .child_process import LockHolder, Platform
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .private_path import FileIdentity, PrivateFileRole


class LockMode(ABC):
    """Whether a store lock lets other holders in, and what its holder may do."""

    @abstractmethod
    def flock(self) -> int:
        """This mode's POSIX flock operation."""

    @abstractmethod
    def require_writer(self) -> None:
        """Require the exclusive holder before rebuilding a replaced source."""

    def require_held(self, held: LockMode) -> None:
        pass


class SharedLock(LockMode):
    """Concurrent readers of the owning store."""

    def flock(self) -> int:
        return fcntl.LOCK_SH

    def require_writer(self) -> None:
        raise RelationViolationError("Cannot upgrade an enclosing shared store lock.")


class ExclusiveLock(LockMode):
    """The owning store's exclusive writer."""

    def flock(self) -> int:
        return fcntl.LOCK_EX

    def require_writer(self) -> None:
        pass

    def require_held(self, held: LockMode) -> None:
        held.require_writer()


class StoreLockBusyError(BlockingIOError):
    """A store lock exhausted its wait budget, with the observed holders."""

    holders: tuple[LockHolder, ...]

    def __init__(self, holders: tuple[LockHolder, ...]) -> None:
        super().__init__(errno.EAGAIN, "store lock busy")
        self.holders = holders

    def __str__(self) -> str:
        return "store lock busy, held by " + "; ".join(holder.describe() for holder in self.holders)


@dataclass(slots=True)
class StoreLockContention:
    """One operation's contention budget, spent only while acquiring physical locks."""

    remaining: float

    def waits(self, descriptor: int, platform: Platform, *, mode: LockMode) -> Iterator[float]:
        """One physical acquisition algorithm; drivers own sync/async waiting."""
        while True:
            begun = time.monotonic()
            try:
                platform.try_store_lock(descriptor, mode=mode)
            except BlockingIOError as error:
                self.remaining -= time.monotonic() - begun
                if self.remaining <= 0:
                    raise StoreLockBusyError(platform.lock_holders(descriptor)) from error
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


class UnboundedContention(StoreLockContention):
    """The budget that never runs out: spending from it leaves it unbounded, so one
    declared instance serves every caller that waits as long as the lock is held."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(math.inf)


UNBOUNDED_CONTENTION = UnboundedContention()


@dataclass
class StoreLockRelease:
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
            self.active = False
            self.actions.close()


@dataclass(frozen=True)
class StoreLock:
    descriptor: int
    _releases: ClassVar[ContextVar[StoreLockRelease]] = ContextVar(
        "store_lock_release", default=StoreLockRelease(active=False)
    )

    @classmethod
    @contextmanager
    def hold(cls, path: Path, descriptor: int, mode: LockMode):
        yield cls(descriptor)

    @classmethod
    @contextmanager
    def ordered(
        cls, requests: Mapping[StoreLockOwner, LockMode], *,
        contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ):
        """Acquire store owners by their declared rank, never their filenames."""
        with ExitStack() as resources:
            yield {owner: resources.enter_context(owner.locked(
                mode=requests[owner], contention=contention))
                for owner in sorted(requests, key=lambda owner: (
                    type(owner).lock_rank(), str(owner.path)))}

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
        """Share outer retirement; inherited contexts observe that same scope ending."""
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


class StoreReadBudget:
    """The unbounded source-read state."""

    def check(self, path: Path) -> None:
        pass


@dataclass(frozen=True)
class BoundedStoreReadBudget(StoreReadBudget):
    maximum: int

    def __post_init__(self) -> None:
        if type(self.maximum) is not int or self.maximum < 0:
            raise ValueError("bus read cap must be a nonnegative integer")

    def check(self, path: Path) -> None:
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.maximum:
                raise RelationViolationError("Bus exceeds bounded read budget.")


UNBOUNDED_STORE_READ = StoreReadBudget()


class StoreOwner(DeclaredFamily):
    __slots__ = ()
    staging_name: ClassVar[str] = ".{name}.{token}"
    staging_suffix: ClassVar[str] = ".tmp"
    rollback_suffix: ClassVar[str] = ".previous"

    @classmethod
    def stage_file(cls, path: Path) -> tuple[int, Path]:
        descriptor, name = tempfile.mkstemp(
            prefix=cls.staging_name.format(name=path.name, token=""),
            suffix=cls.staging_suffix,
            dir=path.parent,
        )
        return descriptor, Path(name)

    @staticmethod
    def snapshot_lock(path: Path) -> Path:
        return path.with_name(f".{path.name}.snapshot-lock")

    @staticmethod
    def snapshot_intent(path: Path) -> Path:
        return path.with_name(f".{path.name}.pending")

    @classmethod
    def rollback_file(cls, staged: Path) -> Path:
        return staged.with_suffix(cls.rollback_suffix)

    @classmethod
    def is_staging(cls, path: Path) -> bool:
        pattern = cls.staging_name.format(name="*", token="*")
        return any(
            fnmatchcase(path.name, pattern + suffix)
            for suffix in (cls.staging_suffix, cls.rollback_suffix)
        )


class StoreLockFile(StoreOwner, declared_name=None):
    """A file whose writers take the store lock inode beside it, so that inode is
    this file's live state, never a leftover."""

    __slots__ = ()


class StoreLockOwner(StoreLockFile, declared_name=None):
    __slots__ = ()
    path: Path
    lock_type: ClassVar[type[StoreLock]] = StoreLock

    @classmethod
    def lock_tiers(cls) -> tuple[type[StoreLockOwner], ...]:
        """Direct tier declarations, in this module's family-registration order."""
        return tuple(owner for owner in StoreLockOwner.members_with(StoreLockTier)
                     if StoreLockTier in owner.__bases__)

    @classmethod
    def lock_rank(cls) -> int:
        tiers = StoreLockOwner.lock_tiers()
        inherited = tuple(tier for tier in tiers if tier in cls.__mro__)
        if len(inherited) != 1:
            raise TypeError(f"{cls.__qualname__} must inherit exactly one store lock tier")
        return tiers.index(inherited[0])

    @classmethod
    def validate_selection(cls, members: Iterable[type[DeclaredFamily]]) -> None:
        # A tier registers before it can rank its concrete stores. Store declarations
        # already see all tiers because this module finishes before theirs imports.
        if StoreLockTier not in cls.__bases__:
            cls.lock_rank()

    def locked(self, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
               mode: LockMode = ExclusiveLock(),
               contention: StoreLockContention = UNBOUNDED_CONTENTION):
        return _store_lock(self.path, read_budget=read_budget,
                           mode=mode, contention=contention, lock_type=self.lock_type)

    def locked_async(self) -> AbstractAsyncContextManager[StoreLock]:
        return _async_store_lock(self.path, lock_type=self.lock_type)


class StoreLockTier(StoreLockOwner, declared_name=None):
    """Rank capabilities in the existing owner family, without a file of their own."""

    __slots__ = ()


class SourceTier(StoreLockTier):
    """HistoryArchive.attach holds history before registry (historical_views.py:472/492)."""

    __slots__ = ()


class RegistryTier(StoreLockTier):
    """Tag updates hold registry before catalog (channel_management.py:191/303)."""

    __slots__ = ()


class CatalogTier(StoreLockTier):
    """CapturedSelection holds catalog before reads (read_basis.py:1127/1128)."""

    __slots__ = ()


class DependentStateTier(StoreLockTier):
    """Reads follow catalog; rename/delete state follows registry (thread_management.py:397)."""

    __slots__ = ()


def store_lock_path(store_path: Path) -> Path:
    """The original named inode for one store's physical lock."""
    return store_path.with_name(f'.{store_path.name}.lock')


def locked_store_path(lock_path: Path) -> Path:
    """The store ``store_lock_path`` would name ``lock_path`` for; inverse of that function."""
    return lock_path.with_name(lock_path.name.removeprefix('.').removesuffix('.lock'))


def is_store_lock(path: Path) -> bool:
    """Whether ``path`` is the lock ``store_lock_path`` names, present or not its store."""
    store = locked_store_path(path)
    return store != path and store_lock_path(store) == path


@contextmanager
def _store_lock_file(store_path: Path):
    """Open read-only: lock retirement must not generate a close-write watch event."""
    if is_store_lock(store_path):
        raise ValueError(f"A store lock is never itself a lockable store: {store_path}")
    store_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = store_lock_path(store_path)
    with StoreLock.releasing(), os.fdopen(
        os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o666), "rb"
    ) as lock_file:
        yield lock_file


@dataclass(frozen=True)
class SynchronousStoreExecution:
    pass


@dataclass(frozen=True)
class StoreLockExecution:
    pid: int
    thread: int
    task: asyncio.Task | SynchronousStoreExecution


class UnacquiredStoreLock:
    @contextmanager
    def locked(
        self, store_path: Path, read_budget: StoreReadBudget, mode: LockMode,
        contention: StoreLockContention, lock_type: type[StoreLock],
    ) -> Iterator[StoreLock]:
        platform = Platform.current()
        with _store_lock_file(store_path) as lock_file:
            contention.acquire(lock_file.fileno(), platform, mode=mode)
            with StoreLockAcquisition.holding(
                store_path, lock_file, platform, read_budget, mode=mode, lock_type=lock_type,
            ) as lock:
                yield lock

    @asynccontextmanager
    async def locked_async(
        self, store_path: Path, read_budget: StoreReadBudget, mode: LockMode,
        contention: StoreLockContention, lock_type: type[StoreLock],
    ) -> AsyncIterator[StoreLock]:
        platform = Platform.current()
        with _store_lock_file(store_path) as lock_file:
            await contention.acquire_async(lock_file.fileno(), platform, mode=mode)
            with StoreLockAcquisition.holding(
                store_path, lock_file, platform, read_budget, mode=mode, lock_type=lock_type,
            ) as lock:
                yield lock


@dataclass
class StoreLockAcquisition:
    file: BinaryIO
    mode: LockMode
    identity: FileIdentity
    _held: ClassVar[ContextVar[tuple[tuple[StoreLockExecution, StoreLockAcquisition], ...]]] = (
        ContextVar("store_lock_acquisitions", default=())
    )

    @staticmethod
    def execution() -> StoreLockExecution:
        task: asyncio.Task | SynchronousStoreExecution = SynchronousStoreExecution()
        match asyncio._get_running_loop():
            case None:
                pass
            case loop:
                match asyncio.current_task(loop):
                    case asyncio.Task() as running:
                        task = running
        return StoreLockExecution(os.getpid(), threading.get_ident(), task)

    @classmethod
    def current(
        cls, store_path: Path, mode: LockMode,
    ) -> StoreLockAcquisition | UnacquiredStoreLock:
        execution = cls.execution()
        try:
            identity = FileIdentity.from_stat(store_lock_path(store_path).stat())
        except FileNotFoundError:
            return UnacquiredStoreLock()
        for borrower, acquisition in reversed(cls._held.get()):
            if (borrower == execution and acquisition.identity == identity
                    and not acquisition.file.closed):
                mode.require_held(acquisition.mode)
                return acquisition
        return UnacquiredStoreLock()

    def locked(
        self, store_path: Path, read_budget: StoreReadBudget, mode: LockMode,
        contention: StoreLockContention, lock_type: type[StoreLock],
    ) -> AbstractContextManager[StoreLock]:
        return self.source(store_path, read_budget, lock_type)

    @asynccontextmanager
    async def locked_async(
        self, store_path: Path, read_budget: StoreReadBudget, mode: LockMode,
        contention: StoreLockContention, lock_type: type[StoreLock],
    ) -> AsyncIterator[StoreLock]:
        with self.source(store_path, read_budget, lock_type) as lock:
            yield lock

    @classmethod
    @contextmanager
    def holding(
        cls, store_path: Path, file: BinaryIO, platform: Platform, read_budget: StoreReadBudget,
        *, mode: LockMode, lock_type: type[StoreLock],
    ) -> Iterator[StoreLock]:
        with ExitStack() as resources:
            acquisition = cls(file, mode, FileIdentity.from_stat(os.fstat(file.fileno())))
            yield resources.enter_context(acquisition.source(store_path, read_budget, lock_type))

    @contextmanager
    def source(
        self, store_path: Path, read_budget: StoreReadBudget, lock_type: type[StoreLock],
    ) -> Iterator[StoreLock]:
        """Verify and lend the source, including to an explicitly joined worker."""
        if self.file.closed:
            raise RelationViolationError("Store lock acquisition has ended.")
        token = self._held.set((*self._held.get(), (self.execution(), self)))
        try:
            read_budget.check(store_path)
            with lock_type.hold(store_path, self.file.fileno(), self.mode) as lock:
                yield lock
        finally:
            self._held.reset(token)


def _store_lock(
    store_path: Path,
    *,
    read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    mode: LockMode = ExclusiveLock(),
    contention: StoreLockContention = UNBOUNDED_CONTENTION,
    lock_type: type[StoreLock] = StoreLock,
) -> AbstractContextManager[StoreLock]:
    return StoreLockAcquisition.current(store_path, mode).locked(
        store_path, read_budget, mode, contention, lock_type,
    )


def _async_store_lock(
    store_path: Path,
    *,
    read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    mode: LockMode = ExclusiveLock(),
    contention: StoreLockContention = UNBOUNDED_CONTENTION,
    lock_type: type[StoreLock] = StoreLock,
) -> AbstractAsyncContextManager[StoreLock]:
    return StoreLockAcquisition.current(store_path, mode).locked_async(
        store_path, read_budget, mode, contention, lock_type,
    )


@dataclass(frozen=True)
class AtomicFile:
    """A staged replacement always syncs its contents and directory entry."""

    path: Path
    mode: int = PrivateFileRole.permissions

    @contextmanager
    def staged(self) -> Iterator[BinaryIO]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = StoreOwner.stage_file(self.path)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.chmod(temporary, self.mode)
                yield output
                output.flush()
                os.fsync(output.fileno())
            self._publish(temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def write_text(self, text: str) -> None:
        self.write_bytes(text.encode("utf-8"))

    def write_bytes(self, data: bytes) -> None:
        with self.staged() as output:
            output.write(data)

    def replace(self, staged: Path) -> None:
        """Publish an independently prepared file after syncing its data."""
        Platform.current().sync_file(staged)
        self._publish(staged)

    def _publish(self, staged: Path) -> None:
        self._install(staged)
        Platform.current().sync_directory(self.path.parent)

    def _install(self, staged: Path) -> None:
        os.replace(staged, self.path)

    def sync(self) -> None:
        """Sync an existing file without replacing an open database's inode."""
        platform = Platform.current()
        platform.sync_file(self.path)
        platform.sync_directory(self.path.parent)


class ExclusiveAtomicFile(AtomicFile):
    """Create a new name atomically, refusing an existing destination."""

    def _install(self, staged: Path) -> None:
        os.link(staged, self.path)


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
