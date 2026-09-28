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
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from .native_entries import NativeEntry, SelectedFreshMarker, SessionEntry
from .native_pi import (
    NativePiUnavailable,
    _durable_private_session_dir,
    _fsync_directory,
    _read_private_file,
    _unique,
)

_MINT = object()
_ENTRY_ID = re.compile(r"[0-9a-f]{8}\Z")
_MAX_STARTUP_APPEND = 2048


@dataclass(frozen=True, init=False, slots=True, weakref_slot=True)
class FreshPrivateSession:
    path: Path
    session_id: str
    device: int
    inode: int
    header_sha256: str
    bootstrap_sha256: str
    bootstrap_size: int
    bootstrap_leaf_id: str | None
    selected_thinking_level: str | None
    creator_pid: int

    def __init__(
        self,
        key: object,
        path: Path,
        session_id: str,
        device: int,
        inode: int,
        header_sha256: str,
        bootstrap_sha256: str,
        bootstrap_size: int,
        bootstrap_leaf_id: str | None,
        selected_thinking_level: str | None,
    ) -> None:
        if key is not _MINT:
            raise TypeError("Fresh-session enrollment cannot be reconstructed from a file")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "inode", inode)
        object.__setattr__(self, "header_sha256", header_sha256)
        object.__setattr__(self, "bootstrap_sha256", bootstrap_sha256)
        object.__setattr__(self, "bootstrap_size", bootstrap_size)
        object.__setattr__(self, "bootstrap_leaf_id", bootstrap_leaf_id)
        object.__setattr__(self, "selected_thinking_level", selected_thinking_level)
        object.__setattr__(self, "creator_pid", os.getpid())

    def __reduce__(self) -> NoReturn:
        raise TypeError("Fresh-session enrollment cannot cross a process boundary")

    @staticmethod
    def require_launch_header(path: Path, selected_thinking_level: str | None) -> None:
        """A saved marker can deny reopen; it cannot recreate first-start authority."""
        rows = _read_private_file(path)
        try:
            header = NativeEntry.from_evidence(rows[0])
            if not isinstance(header, SessionEntry):
                raise ValueError("Native source lacks a session header")
            header.require_header()
            expected = (
                SelectedFreshMarker(1, selected_thinking_level)
                if selected_thinking_level is not None
                else None
            )
            if header.selected_fresh != expected:
                raise ValueError("Selected fresh source lacks its exact first-start token")
        except (IndexError, ValueError, TypeError, KeyError) as error:
            raise NativePiUnavailable(
                "Selected fresh source cannot reopen without exact first-start token"
            ) from error

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
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_nlink != 1
                    or opened.st_size < self.bootstrap_size
                ):
                    raise NativePiUnavailable("Fresh-session opened identity changed")
                bootstrap = bytearray()
                while len(bootstrap) < self.bootstrap_size:
                    chunk = os.read(descriptor, self.bootstrap_size - len(bootstrap))
                    if not chunk:
                        raise NativePiUnavailable("Fresh-session bootstrap became incomplete")
                    bootstrap.extend(chunk)
            finally:
                os.close(descriptor)
            header = bytes(bootstrap).split(b"\n", 1)[0] + b"\n"
            if (
                not header.endswith(b"\n")
                or hashlib.sha256(header).hexdigest() != self.header_sha256
                or hashlib.sha256(bootstrap).hexdigest() != self.bootstrap_sha256
            ):
                raise NativePiUnavailable("Fresh-session bootstrap changed")
            if prewrite and info.st_size != self.bootstrap_size:
                raise NativePiUnavailable("Fresh-session has earlier input before enrollment")
            row = NativeEntry.from_evidence(json.loads(header))
            after = self.path.lstat()
            if (after.st_dev, after.st_ino, after.st_nlink) != (self.device, self.inode, 1) or (
                prewrite and after.st_size != self.bootstrap_size
            ):
                raise NativePiUnavailable("Fresh-session path changed after bootstrap read")
            if (
                not isinstance(row, SessionEntry)
                or row.id != self.session_id
                or row.selected_fresh
                != (
                    SelectedFreshMarker(1, self.selected_thinking_level)
                    if self.selected_thinking_level is not None
                    else None
                )
            ):
                raise NativePiUnavailable("Fresh-session header identity changed")
        except (OSError, ValueError, TypeError) as error:
            raise NativePiUnavailable(
                "Fresh-session header cannot prove new-file identity"
            ) from error

    def verify_prewrite(self) -> None:
        self.verify_saved_identity(prewrite=True)

    def verify_selected_startup(self) -> tuple[int, int, int, int, int]:
        """Attest exactly Pi's two expected metadata appends, no raw messages.

        Pinned createAgentSession appends its initial model and thinking entries
        on a zero-message session, even when explicit launch flags are supplied.
        This is a file observation, not provider/child/terminal authority.
        """
        if self.selected_thinking_level not in {"low", "high"} or not self.bootstrap_leaf_id:
            raise NativePiUnavailable("Fresh source lacks explicit selected bootstrap")
        self.verify_saved_identity()
        try:
            info = self.path.lstat()
            tail_size = info.st_size - self.bootstrap_size
            if not 0 < tail_size <= _MAX_STARTUP_APPEND:
                raise NativePiUnavailable("Selected startup has no exact metadata tail")
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_nlink != 1
                    or (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_size,
                        opened.st_mtime_ns,
                        opened.st_ctime_ns,
                    )
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    or (opened.st_dev, opened.st_ino) != (self.device, self.inode)
                ):
                    raise NativePiUnavailable("Selected startup inode/revision changed")
                chunks = bytearray()
                while len(chunks) < info.st_size:
                    chunk = os.read(descriptor, info.st_size - len(chunks))
                    if not chunk:
                        raise NativePiUnavailable("Selected startup file became incomplete")
                    chunks.extend(chunk)
                # Pi's startup appendFileSync alone is not a durable receipt.
                # Fail closed on uncertain fsync before the raw prompt writer.
                os.fsync(descriptor)
                synced = os.fstat(descriptor)
                if (
                    synced.st_dev,
                    synced.st_ino,
                    synced.st_size,
                    synced.st_mtime_ns,
                    synced.st_ctime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ):
                    raise NativePiUnavailable("Selected startup changed across fsync")
            finally:
                os.close(descriptor)
            data = bytes(chunks)
            if hashlib.sha256(
                data[: self.bootstrap_size]
            ).hexdigest() != self.bootstrap_sha256 or not data.endswith(b"\n"):
                raise NativePiUnavailable("Selected startup changed its enrolled prefix")
            lines = data[self.bootstrap_size :].splitlines(keepends=True)
            if len(lines) != 2 or any(not line.endswith(b"\n") for line in lines):
                raise NativePiUnavailable("Selected startup has extra or partial entries")
            model, thinking = (json.loads(line, object_pairs_hook=_unique) for line in lines)
            common = {"type", "id", "parentId", "timestamp"}
            if (
                type(model) is not dict
                or type(thinking) is not dict
                or set(model) != common | {"provider", "modelId"}
                or set(thinking) != common | {"thinkingLevel"}
                or model["type"] != "model_change"
                or model["provider"] != "openrouter"
                or model["modelId"] != "z-ai/glm-5.3-flash"
                or model["parentId"] != self.bootstrap_leaf_id
                or thinking["type"] != "thinking_level_change"
                or thinking["thinkingLevel"] != self.selected_thinking_level
                or thinking["parentId"] != model["id"]
                or any(
                    type(row["id"]) is not str
                    or _ENTRY_ID.fullmatch(row["id"]) is None
                    or type(row["timestamp"]) is not str
                    or not row["timestamp"]
                    for row in (model, thinking)
                )
                or model["id"] == thinking["id"]
                or model["id"] == self.bootstrap_leaf_id
                or thinking["id"] == self.bootstrap_leaf_id
            ):
                raise NativePiUnavailable("Selected startup metadata does not match runtime")
            after = self.path.lstat()
            revision = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) != revision:
                raise NativePiUnavailable("Selected startup revision changed after read")
            return revision
        except (OSError, ValueError, TypeError, KeyError, NativePiUnavailable) as error:
            raise NativePiUnavailable("Selected startup metadata cannot be attested") from error


def create_fresh_private_session(
    session_dir: Path, *, worktree: Path, selected_thinking_level: str | None = None
) -> FreshPrivateSession:
    """Create a unique v3 header with O_EXCL, file fsync, then parent fsync.

    Call only for explicit fresh-session enrollment. An existing path, reused
    ID, failed fsync or uncertain return is not eligible, even if a header is
    later visible. This API never opens an old saved session for enrollment.
    """
    # Explicitly selected static model only; the level may not come from an
    # arbitrary request or be silently clamped from an unsupported 'off'.
    if selected_thinking_level is not None and (
        type(selected_thinking_level) is not str or selected_thinking_level not in {"low", "high"}
    ):
        raise ValueError("Explicit supported selected thinking level required")
    session_dir = Path(session_dir).absolute()
    worktree = Path(worktree).absolute()
    if not worktree.is_dir():
        raise NativePiUnavailable("Fresh-session worktree is unavailable")
    _durable_private_session_dir(session_dir)
    session_id = uuid4().hex
    path = session_dir / f"enrolled-{session_id}.jsonl"
    header_row = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "cwd": str(worktree),
    }
    if selected_thinking_level is not None:
        # This is a denial marker, not authority to enroll or to dispatch.
        # Omission on a later path-only reopen must not bypass the selected
        # first-startup model/thinking gate.
        header_row["agentCommsSelectedFresh"] = SelectedFreshMarker(
            1, selected_thinking_level
        ).to_wire()
    header = json.dumps(header_row, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    bootstrap_leaf_id = None
    bootstrap = header
    if selected_thinking_level is not None:
        model_entry_id = uuid4().hex[:8]
        bootstrap_leaf_id = uuid4().hex[:8]
        while bootstrap_leaf_id == model_entry_id:
            bootstrap_leaf_id = uuid4().hex[:8]
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for row in (
            {
                "type": "model_change",
                "id": model_entry_id,
                "parentId": None,
                "timestamp": timestamp,
                "provider": "openrouter",
                "modelId": "z-ai/glm-5.3-flash",
            },
            {
                "type": "thinking_level_change",
                "id": bootstrap_leaf_id,
                "parentId": model_entry_id,
                "timestamp": timestamp,
                "thinkingLevel": selected_thinking_level,
            },
        ):
            bootstrap += json.dumps(row, separators=(",", ":"), allow_nan=False).encode() + b"\n"
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
            pending = memoryview(bootstrap)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError("Fresh-session bootstrap write made no progress")
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
            hashlib.sha256(bootstrap).hexdigest(),
            len(bootstrap),
            bootstrap_leaf_id,
            selected_thinking_level,
        )
        result.verify_prewrite()
        return result
    except OSError as error:
        # Do not delete a possibly committed header after a failed fsync. It
        # remains unenrolled, never an inferred coverage grant.
        raise NativePiUnavailable("Fresh-session creation durability UNKNOWN") from error
