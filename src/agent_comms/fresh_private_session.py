"""Create an ordinary saved native session before registering its worker."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_comms.child_process import Platform
from .field_codec import FieldCodec, RecordedTimestamp
from .native_entries import SessionEntry
from .native_failures import NativePiUnavailable
from agent_comms.native_session_files import NativeSessionFiles
from .native_session_files import PrivateSession, BusTranscriptFile
from .native_session_reopen import NativeSessionIdentity
from .private_path import PrivateFileRole


def create_fresh_private_session(
    root: Path, *, worktree: Path, location: type[BusTranscriptFile] = PrivateSession, **parts: str
) -> NativeSessionIdentity:
    """Exclusively create and sync the SDK header; never reuse an uncertain file."""
    worktree = Path(worktree).absolute()
    if not worktree.is_dir():
        raise NativePiUnavailable("Fresh-session worktree is unavailable")
    session_id = uuid4().hex
    path = location.path_in(Path(root).absolute(), session_id=session_id, **parts)
    session_dir = path.parent
    NativeSessionFiles.prepare_private_directory(session_dir)
    header = SessionEntry(
        id=session_id, version=3, cwd=str(worktree),
        timestamp=RecordedTimestamp.written(datetime.now(UTC)),
    )
    encoded = FieldCodec.compact(header.to_wire(), allow_nan=False).encode() + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            PrivateFileRole.require(info, NativePiUnavailable("Fresh-session creation durability UNKNOWN"))
            pending = memoryview(encoded)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError("Fresh-session write made no progress")
                pending = pending[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        Platform.current().sync_directory(session_dir)
        return NativeSessionIdentity(session_id, str(path))
    except OSError as error:
        # A failed sync can leave a committed file. Preserve it, never infer success.
        raise NativePiUnavailable("Fresh-session creation durability UNKNOWN") from error
