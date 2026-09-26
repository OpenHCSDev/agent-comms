"""Explicit fresh Pi session enrollment with a durable pre-prompt file identity.

A normal Pi new session has only a *future path* before its first prompt. The
reviewed SessionManager opens an explicitly supplied valid session header with
``--session``, retaining that inode when it appends messages. A file created by
this helper is not by itself selected-summary permission: the owner must also
register coverage under its wire/registry/store locks and settle every raw
input with a returned terminal receipt. An interrupted creation is never
reconstructed into an enrollment from disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from .native_pi import NativePiUnavailable, _durable_private_session_dir, _fsync_directory

_MINT = object()


@dataclass(frozen=True, init=False, slots=True, weakref_slot=True)
class FreshPrivateSession:
    path: Path
    session_id: str
    device: int
    inode: int
    header_sha256: str
    creator_pid: int

    def __init__(
        self, key: object, path: Path, session_id: str, device: int, inode: int, header_sha256: str
    ) -> None:
        if key is not _MINT:
            raise TypeError("Fresh-session enrollment cannot be reconstructed from a file")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "inode", inode)
        object.__setattr__(self, "header_sha256", header_sha256)
        object.__setattr__(self, "creator_pid", os.getpid())

    def __reduce__(self) -> NoReturn:
        raise TypeError("Fresh-session enrollment cannot cross a process boundary")

    def verify_saved_identity(self, *, prewrite: bool = False) -> None:
        """Verify the original header/inode, optionally requiring no Pi appends yet."""
        if os.getpid() != self.creator_pid:
            raise NativePiUnavailable("Fresh-session creator process changed")
        try:
            info = self.path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (self.device, self.inode)
                or info.st_size <= 0
            ):
                raise NativePiUnavailable("Fresh-session saved inode changed")
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (self.device, self.inode):
                    raise NativePiUnavailable("Fresh-session path rebound while opening")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    header = stream.readline(1025)
            finally:
                os.close(descriptor)
            if (
                not header.endswith(b"\n")
                or hashlib.sha256(header).hexdigest() != self.header_sha256
            ):
                raise NativePiUnavailable("Fresh-session header changed")
            if prewrite and info.st_size != len(header):
                raise NativePiUnavailable("Fresh-session has earlier input before enrollment")
            row = json.loads(header)
            after = self.path.lstat()
            if (after.st_dev, after.st_ino, after.st_nlink) != (self.device, self.inode, 1):
                raise NativePiUnavailable("Fresh-session path changed after header read")
            if (
                type(row) is not dict
                or row.get("type") != "session"
                or row.get("id") != self.session_id
            ):
                raise NativePiUnavailable("Fresh-session header identity changed")
        except (OSError, ValueError, TypeError) as error:
            raise NativePiUnavailable(
                "Fresh-session header cannot prove new-file identity"
            ) from error

    def verify_prewrite(self) -> None:
        self.verify_saved_identity(prewrite=True)


def create_fresh_private_session(session_dir: Path, *, worktree: Path) -> FreshPrivateSession:
    """Create a unique v3 header with O_EXCL, file fsync, then parent fsync.

    Call only for explicit fresh-session enrollment. An existing path, reused
    ID, failed fsync or uncertain return is not eligible, even if a header is
    later visible. This API never opens an old saved session for enrollment.
    """
    session_dir = Path(session_dir).absolute()
    worktree = Path(worktree).absolute()
    if not worktree.is_dir():
        raise NativePiUnavailable("Fresh-session worktree is unavailable")
    _durable_private_session_dir(session_dir)
    session_id = uuid4().hex
    path = session_dir / f"enrolled-{session_id}.jsonl"
    header = (
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "cwd": str(worktree),
            },
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise NativePiUnavailable("Fresh-session file identity is not exclusive")
            pending = memoryview(header)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError("Fresh-session header write made no progress")
                pending = pending[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(session_dir)
        result = FreshPrivateSession(
            _MINT,
            path,
            session_id,
            info.st_dev,
            info.st_ino,
            hashlib.sha256(header).hexdigest(),
        )
        result.verify_prewrite()
        return result
    except OSError as error:
        # Do not delete a possibly committed header after a failed fsync. It
        # remains an unenrolled legacy file, never an inferred coverage grant.
        raise NativePiUnavailable("Fresh-session creation durability UNKNOWN") from error
