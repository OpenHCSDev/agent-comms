"""Reading a private native session's evidence, below the launch that writes it."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from .errors import RelationViolationError
from .nominal.field_codec import FieldCodec, MalformedRecord
from .native_failures import NativePiUnavailable
from .pi_vocabulary import PendingStopReason
from .private_path import FileIdentity, FileRevision, PrivateFileRole, TrustedAncestorRole


class NativeInputUncommitted(NativePiUnavailable):
    """The observed session holds no durable commit of the asked input; its turn is pending."""

    @property
    def stop_reason(self) -> type[PendingStopReason]:
        return PendingStopReason


class NativeEvidencePrefixChanged(NativePiUnavailable):
    """The retained verified prefix must be reacquired from byte zero."""


class NativeEvidenceRemoved(NativePiUnavailable):
    """The recorded recipient session no longer exists at its owned path."""


class UnverifiedNativeEvidencePrefix:
    """No captured cut yet; the private writer owns append-only content."""

    size = 0

    @staticmethod
    def prepare(source: PrivateEvidenceRead, revision: FileRevision) -> FileRevision:
        return revision

    @staticmethod
    def commit(source: PrivateEvidenceRead, size: int, revision: FileRevision) -> FileRevision:
        return source.verify_snapshot(size, revision)


@dataclass(frozen=True, slots=True)
class VerifiedNativeEvidencePrefix(UnverifiedNativeEvidencePrefix):
    """The retained cut and revision of one acquired private inode."""

    size: int = field()
    revision: FileRevision

    def prepare(self, source: PrivateEvidenceRead, revision: FileRevision) -> FileRevision:
        return source.verify_snapshot(self.size, self.revision)


class PrivateEvidenceRead:
    """Pinned private source with one owned verified-prefix state.

    This reader holds its descriptor until close. The private writer owns its
    append-only bytes. Reads check the inode, captured size and complete final
    record, then decode only beyond their consumed position. No history is hashed.
    """

    def __init__(self, path: Path, stream):
        self.path, self.stream = path, stream
        self.identity = FileIdentity.from_stat(os.fstat(stream.fileno()))
        self.prefix = UnverifiedNativeEvidencePrefix()
        self.decoded_through = stream.tell()

    @property
    def size(self) -> int:
        return self.prefix.size

    @classmethod
    def _open_stream(cls, path: Path):
        stream = None
        try:
            if path != path.resolve(strict=True):
                raise NativePiUnavailable("Native Pi evidence session path is not canonical")
            stream = os.fdopen(PrivateFileRole.read(
                path, NativePiUnavailable("Native Pi evidence file is unavailable")), "rb")
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
    def open(cls, path: Path):
        source = cls(path, cls._open_stream(path))
        try:
            yield source
        finally:
            source.close()

    def close(self):
        self.stream.close()

    def reverify(self) -> None:
        """Reopen the source; its reader restores the required input position."""
        self.close()
        self.stream = self._open_stream(self.path)
        self.identity = FileIdentity.from_stat(os.fstat(self.stream.fileno()))
        self.prefix = UnverifiedNativeEvidencePrefix()
        self.decoded_through = self.stream.tell()

    def _capture_append(self):
        """Capture a complete private cut without reading its prior bytes."""
        try:
            info = os.fstat(self.stream.fileno())
            revision = self._observe_revision_after(FileRevision.from_stat(info))
            if not info.st_size:
                raise NativePiUnavailable("Native Pi evidence file is empty")
            if info.st_size < self.size:
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence verified prefix was truncated"
                )
            revision = self.prefix.prepare(self, revision)
            if info.st_size > self.size:
                last = os.pread(self.stream.fileno(), 1, info.st_size - 1)
                if last != b"\n":
                    raise NativePiUnavailable("Native Pi evidence file is incomplete")
            observed = self._observe_revision_after(revision)
            if not revision.holds_cut(observed, info.st_size):
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence source changed during observation"
                )
            return info.st_size, revision
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error

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
                decoder.seek(self.decoded_through)
            except (OSError, ValueError) as error:
                raise NativePiUnavailable(
                    "Native Pi evidence file is unavailable or not private"
                ) from error
            remaining = size - self.decoded_through
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
            row = FieldCodec.parse(raw)
        except MalformedRecord as error:
            raise NativePiUnavailable("Native Pi evidence JSON is invalid") from error
        if type(row) is not dict:
            raise NativePiUnavailable("Native Pi evidence row has wrong type")
        return row

    def header(self):
        """Return row one after verifying the complete captured byte prefix."""
        try:
            size, revision = self._capture_append()
            self.stream.seek(0)
            raw = self.stream.readline(size)
            if not raw or not raw.endswith(b"\n"):
                raise NativePiUnavailable("Native Pi evidence header is incomplete")
            header = self._decode_row(raw)
            self._commit_snapshot(size, revision)
            return header
        except BaseException:
            self.close()
            raise

    def consume_header(self):
        """Consume only record one; verification and decode positions are distinct."""
        header = self.header()
        self.decoded_through = max(self.decoded_through, self.stream.tell())
        return header

    def verify(self) -> None:
        """Validate this acquired source without decoding or moving its input position."""
        size, revision = self._capture_append()
        self._commit_snapshot(size, revision)

    def start_at(self, offset: int) -> None:
        """Seek to a recorded boundary after checking the original source revision."""
        FieldCodec.decode(int, offset)
        size, revision = self._capture_append()
        if not 0 < offset < size or os.pread(self.stream.fileno(), 1, offset - 1) != b'\n':
            raise NativePiUnavailable('Native input byte offset is not a complete source boundary')
        self._commit_snapshot(size, revision)
        self.decoded_through = offset

    def _commit_snapshot(self, size, revision) -> None:
        try:
            observed = self.prefix.commit(self, size, revision)
        except (OSError, ValueError) as error:
            raise NativePiUnavailable(
                "Native Pi evidence file is unavailable or not private"
            ) from error
        self.prefix = VerifiedNativeEvidencePrefix(size, observed)

    def rows(self):
        try:
            previous_size = self.decoded_through
            size, revision = self._capture_append()
            if size == previous_size:
                self.prefix = VerifiedNativeEvidencePrefix(size, revision)
                return
            yield from self._decode_captured(size)
            self._commit_snapshot(size, revision)
            self.decoded_through = size
        except BaseException:
            self.close()
            raise

    def verify_snapshot(self, size, revision):
        """Keep the acquired inode and captured cut; trust our append-only writer."""
        observed = self._observe_revision_after(revision)
        if observed.identity != self.identity or observed.size < size:
            raise NativeEvidencePrefixChanged("Native Pi evidence snapshot source changed")
        return observed

    def _observe_revision_after(self, revision):
        """Check original private inode and monotonic growth, not byte integrity.

        Each observation fences the next: growth seen on the descriptor cannot
        be hidden by shrinking the named file back.
        """
        refusal = NativePiUnavailable("Native Pi evidence has an unsafe ancestor")
        try:
            TrustedAncestorRole.require_path(self.path.parent, refusal)
        except OSError as error:
            raise refusal from error
        # Detect replacement before classifying the old descriptor's now-zero
        # links: observing a new named source takes the existing reverify path.
        for info in (self.path.lstat(), os.fstat(self.stream.fileno())):
            PrivateFileRole.require(info, RelationViolationError("Native Pi evidence file is not private"))
            observed = FileRevision.from_stat(info)
            if observed.identity != self.identity or (
                observed != revision and observed.size <= revision.size
            ):
                raise NativeEvidencePrefixChanged(
                    "Native Pi evidence source changed without growth"
                )
            revision = observed
        return revision


def session_location(directory: Path, candidate: str) -> Path:
    if not isinstance(candidate, str) or not candidate:
        raise NativePiUnavailable("Native Pi omitted its session file")
    path = Path(candidate)
    if not path.is_absolute() or path.parent != directory or path.suffix != ".jsonl":
        raise NativePiUnavailable("Native Pi changed its private session location")
    return path
