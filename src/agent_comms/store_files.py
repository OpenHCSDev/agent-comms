"""Store files: declaration and persistence owners."""

from __future__ import annotations

import asyncio
import errno
import math
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    closing,
    contextmanager,
)
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, TYPE_CHECKING, BinaryIO, ClassVar, Final, TextIO

from .child_process import LockHolder, Platform
from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import PathText
from .private_path import FileIdentity, PrivateFileRole
from .process_claim import ExclusiveFlock, FlockMode, SharedFlock

if TYPE_CHECKING:
    from sqlite3 import _IsolationLevel

DEFAULT_ISOLATION: Final = "DEFERRED"


class LockMode(FlockMode, ABC):
    """Whether a store lock lets other holders in, and what its holder may do."""

    @abstractmethod
    def require_writer(self) -> None:
        """Require the exclusive holder before rebuilding a replaced source."""

    def require_held(self, held: LockMode) -> None:
        pass


class SharedLock(SharedFlock, LockMode):
    """Concurrent readers of the owning store."""

    def require_writer(self) -> None:
        raise RelationViolationError("Cannot upgrade an enclosing shared store lock.")


class ExclusiveLock(ExclusiveFlock, LockMode):
    """The owning store's exclusive writer."""

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
        return "store lock busy, " + LockHolder.described(self.holders)


@dataclass(slots=True)
class StoreLockContention:
    """One operation's contention budget, spent only while acquiring physical locks."""

    remaining: float

    def waits(self, descriptor: int, platform: Platform, *, mode: LockMode) -> Iterator[float]:
        """One physical acquisition algorithm; drivers own sync/async waiting."""
        while True:
            begun = time.monotonic()
            try:
                mode.attempt(descriptor)
            except BlockingIOError as error:
                self.remaining -= time.monotonic() - begun
                if self.remaining <= 0:
                    raise StoreLockBusyError(platform.lock_holders(descriptor)) from error
                self.contended(descriptor, platform)
                begun = time.monotonic()
                try:
                    yield min(self.remaining, platform.store_lock_interval)
                finally:
                    self.remaining -= time.monotonic() - begun
            else:
                return

    def contended(self, descriptor: int, platform: Platform) -> None:
        """Each refused attempt that keeps waiting; a budget that names its holders overrides it."""

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


class HolderNamingContention(UnboundedContention):
    """Waits as long as the lock is held, and prints its holders once, when the wait passes
    ``after`` seconds, so a slow acquisition names whoever held it up (install 18 waited
    59 s for the registry with nobody named)."""

    __slots__ = ("after", "deadline")

    def __init__(self, after: float) -> None:
        super().__init__()
        self.after = after
        self.deadline = time.monotonic() + after

    def contended(self, descriptor: int, platform: Platform) -> None:
        if time.monotonic() >= self.deadline:
            self.deadline = math.inf
            print(f"store lock still waiting after {self.after:g} s, "
                  f"{LockHolder.described(platform.lock_holders(descriptor))}",
                  file=sys.stderr, flush=True)


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
        cls, *guards: AbstractContextManager[object],
    ):
        """Retain the supplied store guards in the caller's declared order."""
        with ExitStack() as resources:
            for guard in guards:
                resources.enter_context(guard)
            yield

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


class StoreGuard:
    """Explicit-path lock behavior without a file-declaration or wire family."""
    __slots__ = ()
    lock_type: ClassVar[type[StoreLock]] = StoreLock

    @classmethod
    def contention(cls) -> StoreLockContention:
        return UNBOUNDED_CONTENTION

    @classmethod
    @contextmanager
    def read_guard(
        cls, path: Path, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> Iterator[None]:
        with _store_lock(path, SharedLock(), cls.contention(),
                         read_budget=read_budget, lock_type=cls.lock_type):
            yield

    @classmethod
    @contextmanager
    def write_guard(
        cls, path: Path, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> Iterator[StoreLock]:
        with _store_lock(path, ExclusiveLock(), cls.contention(),
                         read_budget=read_budget, lock_type=cls.lock_type) as lock:
            yield lock

    @classmethod
    @asynccontextmanager
    async def read_guard_async(
        cls, path: Path, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> AsyncIterator[None]:
        async with _async_store_lock(path, SharedLock(), cls.contention(),
                                     read_budget=read_budget, lock_type=cls.lock_type):
            yield

    @classmethod
    @asynccontextmanager
    async def write_guard_async(
        cls, path: Path, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> AsyncIterator[StoreLock]:
        async with _async_store_lock(path, ExclusiveLock(), cls.contention(),
                                     read_budget=read_budget, lock_type=cls.lock_type) as lock:
            yield lock


class StoreLockOwner(StoreGuard, StoreLockFile, declared_name=None):
    """File declarations whose guards use their instance path."""

    __slots__ = ()
    path: Path

    def guarded_read(
        self, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> AbstractContextManager[None]:
        return type(self).read_guard(self.path, read_budget=read_budget)

    def guarded_write(
        self, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> AbstractContextManager[StoreLock]:
        return type(self).write_guard(self.path, read_budget=read_budget)

    def guarded_write_async(self) -> AbstractAsyncContextManager[StoreLock]:
        return type(self).write_guard_async(self.path)

    def read_now(self) -> AbstractContextManager[StoreLock]:
        return _store_lock(self.path, SharedLock(), StoreLockContention(0),
                           lock_type=self.lock_type)


class RankedStoreLock(StoreLockOwner, declared_name=None):
    """Guard owners that participate in ordered multi-store reads."""

    __slots__ = ()

    @classmethod
    def lock_tiers(cls) -> tuple[type[RankedStoreLock], ...]:
        """Direct tier declarations, in this module's family-registration order."""
        return tuple(owner for owner in RankedStoreLock.members_with(StoreLockTier)
                     if StoreLockTier in owner.__bases__)

    @classmethod
    def lock_rank(cls) -> int:
        tiers = RankedStoreLock.lock_tiers()
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

    @classmethod
    def rank_key(cls, owner: RankedStoreLock) -> tuple[int, str]:
        return type(owner).lock_rank(), str(owner.path)


class SingleFileStore(StoreLockOwner, declared_name=None):
    """One file published by atomic rename: a read sees the old or new file whole, so it takes no lock."""

    __slots__ = ()

    @classmethod
    @contextmanager
    def read_guard(
        cls, path: Path, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> Iterator[None]:
        read_budget.check(path)
        yield

    @classmethod
    @asynccontextmanager
    async def read_guard_async(
        cls, path: Path, *, read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    ) -> AsyncIterator[None]:
        read_budget.check(path)
        yield


class MultiFileStore(StoreLockOwner, declared_name=None):
    """State spanning several files replaced together: one lock covers the set, and readers share it."""


class StoreLockTier(RankedStoreLock, declared_name=None):
    """Rank capabilities in the existing owner family, without a file of their own."""

    __slots__ = ()


class SourceTier(StoreLockTier):
    """HistoryArchive.attach holds history before registry (historical_views.py:472/492)."""

    __slots__ = ()


class RegistryTier(StoreLockTier):
    """Tag updates hold registry before catalog (channel_management.py:191/303)."""

    __slots__ = ()


class RegistryLocking(MultiFileStore, RegistryTier, declared_name=None):
    """The registry's lock policy: registry.json and its owner guard are replaced together,
    and a wait past 5 s names the holders."""

    @classmethod
    def contention(cls) -> StoreLockContention:
        return HolderNamingContention(5)


class CatalogTier(StoreLockTier):
    """CapturedSelection holds catalog before reads (read_basis.py:1127/1128)."""

    __slots__ = ()


class DependentStateTier(StoreLockTier):
    """Reads follow catalog; rename/delete state follows registry (thread_management.py:397)."""

    __slots__ = ()


class StoreContention(ABC):
    """Typed SQLite busy behavior shared by snapshots and writers."""

    @property
    @abstractmethod
    def timeout(self) -> float: ...

    def reader(self, uri: str) -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=self.timeout)

    def writer(
        self, path: str | os.PathLike[str], *,
        isolation_level: _IsolationLevel = DEFAULT_ISOLATION,
        uri: bool = False, check_same_thread: bool = True,
    ) -> sqlite3.Connection:
        return sqlite3.connect(
            path, timeout=self.timeout, isolation_level=isolation_level,
            uri=uri, check_same_thread=check_same_thread,
        )

    def lock_budget(self) -> StoreLockContention:
        """The same wait spent on the store's file locks: one fresh budget per operation."""
        return StoreLockContention(self.timeout)


@dataclass(frozen=True)
class StoreWait(StoreContention):
    timeout: ClassVar[float] = 5.0


@dataclass(frozen=True)
class StoreBusy(StoreContention):
    """Do not wait when optional display or input work cannot acquire a SQLite lock."""

    timeout: ClassVar[float] = 0.0

    def reader(self, uri: str) -> sqlite3.Connection:
        with CoordinationReadUnavailable.checking():
            return super().reader(uri)


DEFAULT_STORE_CONTENTION = StoreWait()


class SqliteStore(StoreGuard, ABC):
    """A SQLite file serializes connection boundaries, not the SQL between them."""

    __slots__ = ()

    @classmethod
    @abstractmethod
    def writer(
        cls, path: str | os.PathLike[str], contention: StoreContention = DEFAULT_STORE_CONTENTION,
        *, isolation_level: _IsolationLevel = DEFAULT_ISOLATION,
        uri: bool = False, check_same_thread: bool = True,
    ) -> sqlite3.Connection:
        """Open the file family's writer with its declared contention policy and options."""

    @classmethod
    @contextmanager
    def attached(
        cls, path: Path, opening: Callable[[], AbstractContextManager[sqlite3.Connection]],
        contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ) -> Iterator[sqlite3.Connection]:
        """Serialize attachment and retirement, including cold WAL-index initialization.

        Attachment spends the caller's lock budget; retirement always waits. SQLite's own
        transactions retain parallel execution between those two boundaries.
        """
        with ExitStack() as lifetime:
            retirement = lifetime.enter_context(ExitStack())
            with _store_lock(path, ExclusiveLock(), contention):
                connection = lifetime.enter_context(opening())
            lifetime.callback(retirement.enter_context, cls.write_guard(path))
            yield connection

    @classmethod
    @contextmanager
    def retaining(cls, path: Path) -> Iterator[BorrowedStoreLock]:
        """Retain the existing store lock while a child borrows its open description."""
        with cls.write_guard(path) as held:
            yield BorrowedStoreLock(held.descriptor, path)

    @classmethod
    @contextmanager
    def exclusive_writer(
        cls, path: Path, *, isolation_level: _IsolationLevel = None, uri: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Keep other connections out, with transaction semantics declared by the caller."""
        with (cls.write_guard(path),
              closing(cls.writer(path, isolation_level=isolation_level, uri=uri)) as db):
            yield db


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


@dataclass(frozen=True)
class BorrowedStoreLock(StoreLock):
    """A child borrows the parent's held descriptor; it never locks or unlocks it."""

    path: Annotated[Path, PathText]

    @contextmanager
    def joined(self) -> Iterator[StoreLock]:
        with os.fdopen(os.dup(self.descriptor), "rb") as file:
            expected = FileIdentity.from_stat(store_lock_path(self.path).stat())
            if FileIdentity.from_stat(os.fstat(file.fileno())) != expected:
                raise RelationViolationError(f"Borrowed lock does not belong to {self.path}")
            with StoreLockAcquisition.holding(
                self.path, file, Platform.current(), UNBOUNDED_STORE_READ,
                mode=ExclusiveLock(), lock_type=StoreLock,
            ) as held:
                yield held


def _store_lock(
    store_path: Path, mode: LockMode, contention: StoreLockContention,
    *,
    read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
    lock_type: type[StoreLock] = StoreLock,
) -> AbstractContextManager[StoreLock]:
    return StoreLockAcquisition.current(store_path, mode).locked(
        store_path, read_budget, mode, contention, lock_type,
    )


def _async_store_lock(
    store_path: Path, mode: LockMode, contention: StoreLockContention,
    *,
    read_budget: StoreReadBudget = UNBOUNDED_STORE_READ,
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
