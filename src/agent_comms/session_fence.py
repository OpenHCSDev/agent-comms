"""Coordinate agent-comms processes that write the same saved Pi session."""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from .native_session_files import NativeSessionFiles


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _open_lock(path: Path) -> int:
    """Prepare one advisory-lock descriptor on POSIX and Windows."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        return fd
    except BaseException:
        os.close(fd)
        raise


class SessionWriterBusyError(RuntimeError):
    """A native executor already owns the session; do not start another writer."""


@contextmanager
def idle_session_writer_fence(session_file: str) -> Iterator[int]:
    """Claim an idle executor slot before wire/registry/native-entry locks.

    This is the SAME lock used by stream_agent_events, not a second writer
    namespace. Nonblocking admission prevents a registry↔executor deadlock.
    Pass the descriptor to a trusted child and close (never LOCK_UN) so parent
    death cannot release the executor slot while that child can still mutate.
    The runtime must additionally make its idle child unavailable during the
    write and reload it before reuse; this physical lock cannot refresh memory.
    """
    if os.name != "posix":
        raise NotImplementedError("Inherited idle-session authority requires POSIX")
    fd = _open_lock(NativeSessionFiles.of(session_file).writer_lock)
    try:
        try:
            _try_lock(fd)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise SessionWriterBusyError(
                "Native executor active; compaction not dispatched"
            ) from error
        yield fd
    finally:
        os.close(fd)


@asynccontextmanager
async def session_writer_fence(session_file: str | None) -> AsyncIterator[None]:
    """Hold the per-session writer resource through one executor operation."""
    if session_file is None:
        yield
        return
    path = NativeSessionFiles.of(session_file).writer_lock
    fd = _open_lock(path)
    locked = False
    try:
        while not locked:
            try:
                _try_lock(fd)
                locked = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                await asyncio.sleep(0.025)
        yield
    finally:
        if locked:
            _unlock(fd)
        os.close(fd)
