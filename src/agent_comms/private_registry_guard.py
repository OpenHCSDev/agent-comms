"""Default-off private registry durability gate; never a public wire receipt.

This two-slot file must be created and directory-fsynced before the private
marker. It is read only while the registry's process lock is held. A pending or
corrupt slot is deliberately NOT repaired or skipped: an atomic replacement
may already be visible even when its directory fsync failed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from pathlib import Path

_MAGIC = b"ACRG"
_VERSION = 1
_PENDING = 1
_COMMITTED = 2
_MAX_SEQ = (1 << 63) - 1
_MAX_REGISTRY = 32 << 20
_HEADER = struct.Struct("!4sB16sQB32s")
_SIZE = _HEADER.size + 32
_EMPTY = hashlib.sha256(b"absent\0").digest()


def _reject(detail: str) -> None:
    # Import lazily because declarations owns the public exception semantics.
    from .declarations import RelationViolationError

    raise RelationViolationError(f"Private registry guard {detail}")


def registry_digest(path: Path) -> bytes:
    """Hash the exact owned registry bytes, distinguishing absence from empty."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return _EMPTY
    except OSError as error:
        _reject(f"registry cannot be opened: {error.__class__.__name__}")
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > _MAX_REGISTRY
        ):
            _reject("registry is not a bounded owner-only regular file")
        digest = hashlib.sha256(b"present\0")
        total = 0
        while data := os.read(fd, 65536):
            total += len(data)
            if total > _MAX_REGISTRY:
                _reject("registry grew beyond the bounded evidence read")
            digest.update(data)
        if total != info.st_size:
            _reject("registry changed during its evidence read")
        return digest.digest()
    finally:
        os.close(fd)


def _record(root_id: bytes, seq: int, phase: int, digest: bytes) -> bytes:
    header = _HEADER.pack(_MAGIC, _VERSION, root_id, seq, phase, digest)
    return header + hashlib.sha256(b"agent-comms/private-registry-guard/v1\0" + header).digest()


def _decode(data: bytes, root_id: bytes) -> tuple[int, int, bytes]:
    if len(data) != _SIZE:
        _reject("slot has an invalid length")
    header, checksum = data[: _HEADER.size], data[_HEADER.size :]
    if hashlib.sha256(b"agent-comms/private-registry-guard/v1\0" + header).digest() != checksum:
        _reject("slot checksum is invalid")
    magic, version, stored_root, seq, phase, digest = _HEADER.unpack(header)
    if (
        magic != _MAGIC
        or version != _VERSION
        or stored_root != root_id
        or seq > _MAX_SEQ
        or phase not in (_PENDING, _COMMITTED)
    ):
        _reject("slot is not for this protocol root")
    return seq, phase, digest


class PrivateRegistryGuard:
    def __init__(self, registry_path: Path, wire_root_id: str):
        if (
            type(wire_root_id) is not str
            or len(wire_root_id) != 32
            or any(ch not in "0123456789abcdef" for ch in wire_root_id)
        ):
            _reject("root ID is invalid")
        self.registry_path = registry_path
        self.path = registry_path.parent / ".registry-owner-guard"
        self.root_id = bytes.fromhex(wire_root_id)

    def _trusted_root(self) -> None:
        if os.name != "posix" or not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "pread", "pwrite")
        ):
            _reject("requires POSIX directory and fixed-slot durability")
        if ".." in self.registry_path.parts:
            _reject("has an ambiguous root path")
        root = self.registry_path.parent.absolute()
        cursor = root
        while True:
            try:
                info = cursor.lstat()
            except OSError:
                _reject("has a missing directory ancestor")
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not sticky_root)
            ):
                _reject("has an untrusted directory ancestor")
            if cursor == root and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                _reject("root must be owner-only")
            if cursor == cursor.parent:
                return
            cursor = cursor.parent

    def _open(self) -> int:
        self._trusted_root()
        try:
            fd = os.open(self.path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            _reject(f"file is missing or redirected: {error.__class__.__name__}")
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size != 2 * _SIZE
        ):
            os.close(fd)
            _reject("file is not a fixed owner-only regular file")
        return fd

    def _slots(self, fd: int) -> tuple[tuple[int, int, bytes], tuple[int, int, bytes]]:
        # Validate BOTH slots even when the first appears to authorize the
        # registry. Falling back after a torn pending slot is unsafe.
        raw = os.pread(fd, 2 * _SIZE, 0)
        first = _decode(raw[:_SIZE], self.root_id)
        second = _decode(raw[_SIZE:], self.root_id)
        low, high = sorted((first[0], second[0]))
        if (low, high) != (0, 0) and high != low + 1:
            _reject("slot sequence is discontinuous or duplicated")
        if (low, high) == (0, 0) and (first != second or first[1] != _COMMITTED):
            _reject("initial slots disagree")
        return first, second

    def _latest(self, fd: int) -> tuple[int, int, bytes, int]:
        first, second = self._slots(fd)
        index = 0 if first[0] > second[0] else 1
        seq, phase, digest = (first, second)[index]
        return seq, phase, digest, index

    @staticmethod
    def _write(fd: int, index: int, value: bytes) -> None:
        written = os.pwrite(fd, value, index * _SIZE)
        if written != len(value):
            _reject("slot write was incomplete")
        os.fsync(fd)

    def verify(self) -> tuple[int, bytes]:
        fd = self._open()
        try:
            seq, phase, digest, _ = self._latest(fd)
            if phase != _COMMITTED or seq == 0:
                _reject("is pending, not committed")
            if digest != registry_digest(self.registry_path):
                _reject("does not match the registry snapshot")
            return seq, digest
        finally:
            os.close(fd)

    def prepare(self, new_digest: bytes) -> tuple[int, int]:
        fd = self._open()
        try:
            seq, phase, digest, latest_index = self._latest(fd)
            if phase != _COMMITTED or seq == 0 or digest != registry_digest(self.registry_path):
                _reject("cannot prepare from an uncommitted snapshot")
            if seq >= _MAX_SEQ:
                _reject("sequence is exhausted")
            index = 1 - latest_index
            self._write(fd, index, _record(self.root_id, seq + 1, _PENDING, new_digest))
            return seq + 1, index
        finally:
            os.close(fd)

    def commit(self, seq: int, index: int, new_digest: bytes) -> None:
        fd = self._open()
        try:
            current = self._slots(fd)[index]
            if current != (seq, _PENDING, new_digest):
                _reject("pending commitment changed")
            if registry_digest(self.registry_path) != new_digest:
                _reject("replacement differs from pending digest")
            self._write(fd, index, _record(self.root_id, seq, _COMMITTED, new_digest))
        finally:
            os.close(fd)

    def create_pending(self) -> None:
        """Caller holds bus then registry locks; no marker may exist yet."""
        self._trusted_root()
        if self.path.exists() or self.path.is_symlink():
            _reject("already exists before initialization")
        digest = registry_digest(self.registry_path)
        if digest != _EMPTY:
            registry_fd = os.open(self.registry_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(registry_fd)
            finally:
                os.close(registry_fd)
        directory_fd = os.open(
            self.registry_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)  # baseline registry name and existing root
            fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                if (
                    os.pwrite(
                        fd,
                        _record(self.root_id, 0, _COMMITTED, digest) * 2,
                        0,
                    )
                    != 2 * _SIZE
                ):
                    _reject("initial slots were incomplete")
                os.fsync(fd)
                os.fsync(directory_fd)  # guard name is durable before marker
                self._write(fd, 1, _record(self.root_id, 1, _PENDING, digest))
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)

    def commit_initial(self) -> None:
        fd = self._open()
        try:
            first, second = self._slots(fd)
            if first != (0, _COMMITTED, registry_digest(self.registry_path)) or second != (
                1,
                _PENDING,
                first[2],
            ):
                _reject("initial pending record is invalid")
            self._write(fd, 1, _record(self.root_id, 1, _COMMITTED, first[2]))
        finally:
            os.close(fd)
