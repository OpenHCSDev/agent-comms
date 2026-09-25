"""Coordinate agent-comms processes that write the same saved Pi session."""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


def _lock_path(session_file: str) -> Path:
    session = Path(session_file).resolve()
    return session.with_name(f".{session.name}.agent-comms-writer.lock")


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


@asynccontextmanager
async def session_writer_fence(session_file: str | None) -> AsyncIterator[None]:
    """Hold a per-session cross-process writer lock across the Pi child lifetime."""
    if session_file is None:
        yield
        return
    path = _lock_path(session_file)
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
