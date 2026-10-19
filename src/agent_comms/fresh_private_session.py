"""Create an ordinary saved native session before registering its worker."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .native_entries import SessionEntry
from .native_pi import NativePiUnavailable, _durable_private_session_dir
from .native_session_reopen import NativeSessionIdentity
from .private_path import PrivateFileRole, fsync_directory


def create_fresh_private_session(
    session_dir: Path, *, worktree: Path
) -> NativeSessionIdentity:
    """Exclusively create and sync the SDK header; never reuse an uncertain file."""
    session_dir = Path(session_dir).absolute()
    worktree = Path(worktree).absolute()
    if not worktree.is_dir():
        raise NativePiUnavailable("Fresh-session worktree is unavailable")
    _durable_private_session_dir(session_dir)
    session_id = uuid4().hex
    path = session_dir / f"enrolled-{session_id}.jsonl"
    header = SessionEntry(
        id=session_id, version=3, cwd=str(worktree),
        timestamp=datetime.now(UTC),
    )
    encoded = json.dumps(header.to_wire(), separators=(",", ":"), allow_nan=False).encode() + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            PrivateFileRole.require(info)
            if info.st_nlink != 1:
                raise NativePiUnavailable("Fresh-session file identity is not exclusive")
            pending = memoryview(encoded)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError("Fresh-session write made no progress")
                pending = pending[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(session_dir)
        return NativeSessionIdentity(session_id, str(path))
    except OSError as error:
        # A failed sync can leave a committed file. Preserve it, never infer success.
        raise NativePiUnavailable("Fresh-session creation durability UNKNOWN") from error
