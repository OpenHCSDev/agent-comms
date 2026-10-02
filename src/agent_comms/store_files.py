"""Store files: declaration and persistence owners."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TYPE_CHECKING

from .errors import RelationViolationError

if TYPE_CHECKING:
    from .private_bus_checkpoint import CertifiedSourceRead


@dataclass(slots=True)
class StoreLockContention:
    """One operation's borrowed wait resource, spent only at physical locks.

    Source reading, decoding and consumer work cannot spend this resource.
    It grants no owner, input, proof or replay permission.
    """

    remaining: float

    def acquire_posix(self, descriptor: int, mode: int) -> None:
        import fcntl
        while True:
            begun = time.monotonic()
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                self.remaining -= time.monotonic() - begun
                if self.remaining <= 0:
                    raise
                begun = time.monotonic()
                time.sleep(min(self.remaining, 0.025))
                self.remaining -= time.monotonic() - begun
            else:
                return


@dataclass(frozen=True)
class StoreLock:
    """One physical lock's descriptor and opened durability resource."""

    descriptor: int
    source: CertifiedSourceRead | None

    def certified_read(self) -> CertifiedSourceRead:
        if self.source is None:
            raise RelationViolationError("Store has no certified original wire source.")
        return self.source


@contextmanager
def _store_lock(
    store_path: Path,
    *,
    blocking: bool = True,
    max_bus_bytes: int | None = None,
    shared: bool = False,
    contention: StoreLockContention | None = None,
) -> Iterator[StoreLock]:
    """Hold a canonical store lock with its inheritable descriptor/resource.

    Shared document readers can coexist; updates retain exclusive ownership.
    A bounded projection refuses over-budget bus bytes before its durability
    scan. POSIX release is by last close, not LOCK_UN: an inherited descriptor
    retains authority if its parent dies before native mutation finishes.
    """
    if max_bus_bytes is not None and (type(max_bus_bytes) is not int or max_bus_bytes < 0):
        raise ValueError("bus read cap must be a nonnegative integer")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path.with_name(f".{store_path.name}.lock")
    with open(lock_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            # Windows permits locking a byte beyond EOF. Writing a sentinel
            # before taking the lock races with another process holding it.
            lock_file.seek(0)
            while True:
                try:
                    mode = msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK
                    msvcrt.locking(lock_file.fileno(), mode, 1)  # type: ignore[attr-defined]
                    break
                except OSError as error:
                    if not blocking or error.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.01)
        else:
            import fcntl

            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            if contention is None:
                fcntl.flock(lock_file.fileno(), mode | (0 if blocking else fcntl.LOCK_NB))
            else:
                contention.acquire_posix(lock_file.fileno(), mode)
        try:
            # The shared claim bus durability guard may parse the entire log.
            # A bounded projection must refuse over-budget bytes *before* that
            # guard starts; the flock excludes cooperating appends meanwhile.
            if max_bus_bytes is not None and store_path.exists():
                bus_info = store_path.lstat()
                if not stat.S_ISREG(bus_info.st_mode) or bus_info.st_size > max_bus_bytes:
                    raise RelationViolationError("Bus exceeds bounded read budget.")
            from .wire_log import WireLog

            with WireLog(store_path).verify_before_read_unlocked() as source:
                yield StoreLock(lock_file.fileno(), source)
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(),
                    msvcrt.LK_UNLCK,
                    1,  # type: ignore[attr-defined]
                )


def _replace_snapshot(source: Path, target: Path, *, windows: bool = os.name == "nt") -> None:
    """Allow a short-lived Windows reader to release the old snapshot handle."""
    for attempt in range(8):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if not windows or getattr(error, "winerror", None) not in {5, 32} or attempt == 7:
                raise
            time.sleep(min(0.01 * (2**attempt), 0.1))


def _atomic_write_text(path: Path, text: str, *, fsync_parent: bool = False) -> None:
    """Replace a snapshot; private guarded writes also durably sync its name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        _replace_snapshot(temporary_path, path)
        # Windows does not expose directory fsync; Linux-only private claim
        # opt-in still requires the full parent-durability boundary below.
        if fsync_parent and os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def file_revision(path: Path) -> tuple[int, int, int, int] | None:
    """Identity of an on-disk revision, including atomic replacements."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


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
