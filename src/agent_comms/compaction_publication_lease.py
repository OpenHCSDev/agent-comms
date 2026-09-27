"""Short-lived local publication identity fence, not a registry authority token.

The ACP handoff may await a slow client. Hold this separate per-wire kernel
lock, never the registry/wire file lock, while its identity is being delivered.
Identity mutations fail promptly rather than blocking the same event loop.
A process crash releases this ephemeral lock; the outbox row stays pending.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def publication_identity_fence(root: Path, *, nonblocking: bool = False) -> Iterator[None]:
    if os.name != "posix":
        # Journal-backed local publication is disabled on non-POSIX targets;
        # unrelated registry identity mutations remain supported there.
        yield
        return
    import fcntl

    path = root / ".compaction-publication-handoff.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Unsafe compaction publication identity fence")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError as error:
            from .declarations import RelationViolationError

            raise RelationViolationError(
                "Canonical session identity is publishing; retry the identity change"
            ) from error
        yield
    finally:
        os.close(fd)
