"""A saved native session file and the sidecars kept beside it."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import RelationViolationError
from .native_errors import NativePiUnavailable
from .pi_rpc import unique_fields
from .private_path import (
    FileIdentity,
    FileRevision,
    PrivateDirectoryRole,
    PrivateFileRole,
    TrustedAncestorRole,
)


@dataclass(frozen=True, slots=True)
class NativeSessionFiles:
    """One saved session: the transcript, its input-proof journal and writer lock.

    The input proof is a SQLite journal written by the native process; its
    rollback/WAL companions belong to it. Any other file sharing the session's
    name is unexpected and refuses deletion.
    """

    session: Path

    def read_rows(self):
        """Yield strict private evidence rows, checking the same opened revision.

        Historical bytes are not an admission quota. A bounded byte snapshot is
        validated before decoding; callers must exhaust the resulting observation.
        """
        with PrivateEvidenceRead.open(self.session) as evidence:
            yield from evidence.rows()

    def header(self):
        """Decode only row one while the source owner verifies every captured byte."""
        with PrivateEvidenceRead.open(self.session) as evidence:
            return evidence.header()

    @staticmethod
    def require_private_directory(directory: Path) -> None:
        if ".." in directory.parts:
            raise NativePiUnavailable("Native Pi session directory is not lexical")
        for ancestor in (directory, *directory.parents):
            info = ancestor.lstat()
            failed = TrustedAncestorRole.violation(info)
            if failed is not None:
                raise NativePiUnavailable(
                    f"Native Pi session directory has unsafe ancestor: {failed.declared_name}"
                )
        if PrivateDirectoryRole.violation(directory.lstat()) is not None:
            raise NativePiUnavailable("Native Pi session directory must be owner-only")

    @staticmethod
    def sync_directory(path: Path) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def prepare_directory(cls, directory: Path) -> None:
        """Commit each new directory entry before a Pi launch can be returned.

        Re-sync the private ancestor chain even on reuse: a visible directory may
        have survived a failed parent fsync on the previous preparation. Stop at
        the parent of the highest owner-only directory, not at the filesystem root.
        """
        missing: list[Path] = []
        current = directory
        while True:
            try:
                current.lstat()
            except FileNotFoundError:
                missing.append(current)
                current = current.parent
            else:
                break
        try:
            for path in reversed(missing):
                path.mkdir(mode=0o700, exist_ok=True)
                cls.require_private_directory(path)
                cls.sync_directory(path.parent)
            cls.require_private_directory(directory)
            parent = directory.parent
            while True:
                cls.sync_directory(parent)
                info = parent.lstat()
                if parent == parent.parent or PrivateDirectoryRole.violation(info) is not None:
                    break
                parent = parent.parent
        except OSError as error:
            raise NativePiUnavailable(
                "Native Pi session directory could not be committed"
            ) from error

    @classmethod
    def of(cls, session_file: str) -> NativeSessionFiles:
        return cls(Path(session_file).resolve())

    @property
    def input_proof(self) -> Path:
        return Path(f"{self.session}.input-proof")

    @property
    def writer_lock(self) -> Path:
        return self.session.with_name(f".{self.session.name}.agent-comms-writer.lock")

    @property
    def members(self) -> tuple[Path, ...]:
        proof = self.input_proof
        return (
            self.session,
            proof,
            *(proof.with_name(proof.name + suffix) for suffix in ("-journal", "-wal", "-shm")),
            self.writer_lock,
        )

    def present(self) -> tuple[Path, ...]:
        return tuple(path for path in self.members if path.exists() or path.is_symlink())

    def require_only_declared_sidecars(self) -> None:
        directory = self.session.parent
        if not directory.is_dir():
            return
        declared = {path.name for path in self.members}
        stray = sorted(
            entry.name
            for entry in os.scandir(directory)
            if (
                entry.name.startswith(self.session.name)
                or entry.name.startswith(f".{self.session.name}")
            )
            and entry.name not in declared
        )
        if stray:
            raise RelationViolationError(
                f"Saved session {self.session} has undeclared sidecars: {', '.join(stray)}"
            )

    def referencing_processes(self) -> tuple[int, ...]:
        """Processes holding a member open, or naming the session in argv or environment.

        Another user's process cannot be inspected; it also cannot open these
        owner-private files, so it is not counted.
        """
        return tuple(
            int(entry.name)
            for entry in os.scandir("/proc")
            if entry.name.isdigit() and self._referenced_by(entry.path)
        )

    def _referenced_by(self, process: str) -> bool:
        members = {str(path) for path in self.members}
        needle = str(self.session).encode()
        try:
            for name in ("cmdline", "environ"):
                with open(f"{process}/{name}", "rb") as stream:
                    if needle in stream.read():
                        return True
            for descriptor in os.scandir(f"{process}/fd"):
                try:
                    target = os.readlink(descriptor.path)
                except FileNotFoundError:
                    continue
                if target.removesuffix(" (deleted)") in members:
                    return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return False
        return False

    def require_unreferenced(self) -> None:
        self.require_only_declared_sidecars()
        if referencing := self.referencing_processes():
            raise RelationViolationError(
                f"Saved session {self.session} is referenced by processes {list(referencing)}"
            )

    def delete(self) -> int:
        """Unlink every member after proving no process references it; bytes freed."""
        self.require_unreferenced()
        freed = 0
        for path in self.present():
            freed += path.lstat().st_size
            path.unlink()
        if self.session.parent.is_dir():
            self.sync_directory(self.session.parent)
        return freed


class NativeEvidencePrefixChanged(NativePiUnavailable):
    """The retained verified prefix must be reacquired from byte zero."""


class NativeEvidenceRemoved(NativePiUnavailable):
    """The recorded recipient session no longer exists at its owned path."""


class NativeEvidenceDigest(Protocol):
    def copy(self) -> NativeEvidenceDigest: ...

    def update(self, value: bytes) -> None: ...

    def digest(self) -> bytes: ...


class UnverifiedNativeEvidencePrefix:
    """Named start state: no byte from the currently opened inode is trusted."""

    size = 0

    @staticmethod
    def copy_digest():
        return hashlib.sha256()

    @staticmethod
    def prepare(source: PrivateEvidenceRead, revision: FileRevision) -> FileRevision:
        return revision

    @staticmethod
    def commit(
        source: PrivateEvidenceRead,
        size: int,
        digest: NativeEvidenceDigest,
        revision: FileRevision,
    ) -> FileRevision:
        observed = source._observe_revision_after(revision)
        return observed if observed == revision else source.verify_snapshot(size, digest.digest())


@dataclass(frozen=True, slots=True)
class VerifiedNativeEvidencePrefix:
    """The sole retained position, hash, and observed revision for one source."""

    size: int
    digest: NativeEvidenceDigest
    revision: FileRevision

    def copy_digest(self):
        return self.digest.copy()

    def prepare(self, source: PrivateEvidenceRead, revision: FileRevision) -> FileRevision:
        if revision == self.revision:
            return revision
        if revision.size < self.revision.size:
            raise NativeEvidencePrefixChanged("Native Pi evidence observed revision was truncated")
        if revision.size > self.revision.size:
            # A private Pi session has append-only growth. The full-prefix hash
            # additionally detected a same-inode rewrite combined with growth,
            # but only by rereading every old byte. Pinned identity/revision
            # still reject replacement, truncation, and non-growth mutation;
            # the running digest extends across the bytes added here.
            return revision
        return source.verify_snapshot(self.size, self.digest.digest())

    @staticmethod
    def commit(
        source: PrivateEvidenceRead,
        size: int,
        digest: NativeEvidenceDigest,
        revision: FileRevision,
    ) -> FileRevision:
        return source._observe_revision_after(revision)


class PrivateEvidenceRead:
    """Pinned private source with one owned verified-prefix state.

    Resource custody lasts until close. Initial acquisition verifies the full
    prefix. Later observations trust an unchanged pinned revision and extend
    its running digest only across appended bytes; non-growth revision changes
    reverify the retained prefix before it can be reused.
    """

    def __init__(self, path, stream):
        self.path, self.stream = path, stream
        self.identity = FileIdentity.from_stat(os.fstat(stream.fileno()))
        self.prefix = UnverifiedNativeEvidencePrefix()

    @property
    def size(self) -> int:
        return self.prefix.size

    @classmethod
    def _open_stream(cls, path):
        stream = None
        try:
            stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb")
            PrivateFileRole.require(os.fstat(stream.fileno()))
            return stream
        except FileNotFoundError as error:
            if stream is not None:
                stream.close()
            raise NativeEvidenceRemoved("Native Pi evidence session was removed") from error
        except (OSError, ValueError) as error:
            if stream is not None:
                stream.close()
            raise NativePiUnavailable("Native Pi evidence file is unavailable") from error

    @classmethod
    @contextmanager
    def open(cls, path):
        source = cls(path, cls._open_stream(path))
        try:
            yield source
        finally:
            source.close()

    def close(self):
        self.stream.close()

    def reverify_from_start(self) -> None:
        """Enter the named unverified state on the current canonical inode."""
        self.close()
        self.stream = self._open_stream(self.path)
        self.identity = FileIdentity.from_stat(os.fstat(self.stream.fileno()))
        self.prefix = UnverifiedNativeEvidencePrefix()

    def _capture_append(self):
        """Verify and hash original bytes without retaining the complete prefix."""
        try:
            info = os.fstat(self.stream.fileno())
            PrivateFileRole.require(info)
            revision = FileRevision.from_stat(info)
            if revision.identity != self.identity:
                raise NativeEvidencePrefixChanged("Native Pi evidence inode changed")
            if not info.st_size:
                raise NativePiUnavailable("Native Pi evidence file is empty")
            if info.st_size < self.size:
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence verified prefix was truncated"
                )
            revision = self.prefix.prepare(self, revision)
            digest = self.prefix.copy_digest()
            last = b""
            for raw in self._pread_chunks(self.size, info.st_size):
                digest.update(raw)
                last = raw[-1:]
            if info.st_size > self.size and last != b"\n":
                raise NativePiUnavailable("Native Pi evidence file is incomplete")
            observed = self._observe_revision_after(revision)
            if not revision.holds_cut(observed, info.st_size):
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence source changed during observation"
                )
            return info.st_size, digest, revision
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error

    def _pread_chunks(self, start, stop):
        """Read a pinned byte range without touching buffered decode state."""
        descriptor = self.stream.fileno()
        while start < stop:
            raw = os.pread(descriptor, min(stop - start, 128 * 1024), start)
            if not raw:
                raise NativePiUnavailable("Native Pi evidence prefix is incomplete")
            start += len(raw)
            yield raw

    def _decode_captured(self, size):
        """Decode through a fresh buffer over the pinned descriptor each time."""
        try:
            descriptor = os.dup(self.stream.fileno())
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error
        try:
            decoder = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            try:
                decoder.seek(self.size)
            except (OSError, ValueError) as error:
                raise NativePiUnavailable(
                    "Native Pi evidence file is unavailable or not private"
                ) from error
            remaining = size - self.size
            while remaining:
                try:
                    raw = decoder.readline(remaining)
                except (OSError, ValueError) as error:
                    raise NativePiUnavailable(
                        "Native Pi evidence file is unavailable or not private"
                    ) from error
                if not raw or not raw.endswith(b"\n"):
                    raise NativePiUnavailable("Native Pi evidence file is incomplete")
                remaining -= len(raw)
                yield self._decode_row(raw)
        finally:
            decoder.close()

    @staticmethod
    def _decode_row(raw):
        try:
            row = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique_fields)
        except (UnicodeError, ValueError) as error:
            raise NativePiUnavailable("Native Pi evidence JSON is invalid") from error
        if type(row) is not dict:
            raise NativePiUnavailable("Native Pi evidence row has wrong type")
        return row

    def header(self):
        """Return row one after verifying the complete captured byte prefix."""
        try:
            size, digest, revision = self._capture_append()
            self.stream.seek(0)
            raw = self.stream.readline(size)
            if not raw or not raw.endswith(b"\n"):
                raise NativePiUnavailable("Native Pi evidence header is incomplete")
            header = self._decode_row(raw)
            self._commit_snapshot(size, digest, revision)
            return header
        except BaseException:
            self.close()
            raise

    def _commit_snapshot(self, size, digest, revision) -> None:
        try:
            observed = self.prefix.commit(self, size, digest, revision)
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error
        self.prefix = VerifiedNativeEvidencePrefix(size, digest, observed)

    def rows(self):
        try:
            previous_size = self.size
            size, digest, revision = self._capture_append()
            if size == previous_size:
                # The pinned revision is unchanged, or a suspicious non-growth
                # change just passed full verification. No row needs decoding.
                self.prefix = VerifiedNativeEvidencePrefix(size, digest, revision)
                return
            # Decode only the captured append. Initial acquisition rehashes its
            # complete prefix once before trust; retained prefixes extend their
            # digest after a final pinned-revision check.
            yield from self._decode_captured(size)
            self._commit_snapshot(size, digest, revision)
        except BaseException:
            self.close()
            raise

    def verify_snapshot(self, size, expected_digest):
        """Validate the captured prefix after decoding, allowing later appends.

        Rehash the captured range once from the pinned descriptor without
        disturbing buffered decoding. Later appends do not change those bytes;
        a later observation rechecks them again before exposing more rows.
        """
        before = os.fstat(self.stream.fileno())
        PrivateFileRole.require(before)
        revision = self._observe_revision_after(FileRevision.from_stat(before))
        if revision.identity != self.identity or revision.size < size:
            raise NativeEvidencePrefixChanged("Native Pi evidence snapshot source changed")
        digest = hashlib.sha256()
        for raw in self._pread_chunks(0, size):
            digest.update(raw)
        revision = self._observe_revision_after(revision)
        if digest.digest() != expected_digest:
            raise NativeEvidencePrefixChanged("Native Pi evidence original prefix changed")
        return revision

    def _observe_revision_after(self, revision):
        """Check original private inode and monotonic growth, not byte integrity.

        Return the last observation so hashing cannot accept a digest from an
        earlier revision. Each observation also fences the next: growth seen on
        the descriptor cannot be hidden by shrinking the named file back.
        """
        for info in (os.fstat(self.stream.fileno()), self.path.lstat()):
            PrivateFileRole.require(info)
            observed = FileRevision.from_stat(info)
            if observed.identity != self.identity or (
                observed != revision and observed.size <= revision.size
            ):
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence source changed without growth"
                )
            revision = observed
        return revision
