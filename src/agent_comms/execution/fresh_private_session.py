"""Create an ordinary saved native session before registering its worker."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from agent_comms.execution.child_process import Platform
from agent_comms.execution.native_entries import SessionEntry
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_session_files import (
    BusTranscriptFile,
    NativeSessionFiles,
    PrivateSession,
)
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.storage.private_path import PrivateFileRole


def create_fresh_private_session(
    root: Path, *, worktree: Path, location: type[BusTranscriptFile] = PrivateSession, **parts: str
) -> NativeSessionIdentity:
    """Exclusively create and sync the SDK header; never reuse an uncertain file."""
    worktree = Path(worktree).absolute()
    if not worktree.is_dir():
        raise NativePiUnavailable("Fresh-session worktree is unavailable")
    header = SessionEntry.fresh(worktree, datetime.now(UTC))
    session_id = header.id
    path = location.path_in(Path(root).absolute(), session_id=session_id, **parts)
    session_dir = path.parent
    NativeSessionFiles.prepare_private_directory(session_dir)
    encoded = FieldCodec.compact(header.to_wire(), allow_nan=False).encode() + b"\n"
    try:
        with PrivateFileRole.creating(
            path, NativePiUnavailable("Fresh-session creation durability UNKNOWN"),
        ) as descriptor:
            pending = memoryview(encoded)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError("Fresh-session write made no progress")
                pending = pending[written:]
            os.fsync(descriptor)
        Platform.current().sync_directory(session_dir)
        return NativeSessionIdentity(session_id, str(path))
    except OSError as error:
        # A failed sync can leave a committed file. Preserve it, never infer success.
        raise NativePiUnavailable("Fresh-session creation durability UNKNOWN") from error
