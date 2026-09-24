"""Default-off cooperative resource reservations for future message envelopes.

This module is deliberately NOT connected to MessageBus.send. A reservation is
not an appended message, a write fence, authentication, or a wake-cohort claim.
Callers must never report claim/message crash-atomicity from this primitive.

Only existing regular, singly-linked files on a trusted local POSIX filesystem
are supported. The caller supplies a pre-existing, owner-only (0700) claims
root. An incomplete claim or interrupted release blocks rather than repairing
itself; there is no timeout, automatic retry, or orphan reclamation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class ClaimError(ValueError):
    """A reservation request violates the supported cooperative scope."""


class UncertainClaim(ClaimError):  # noqa: N818 - domain-specific result
    """An incomplete/corrupt record or interrupted release needs supervision."""


class ClaimNotOwned(ClaimError):  # noqa: N818 - domain-specific result
    """The proposed release does not match the current owner generation."""


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    resource: str
    owner: str
    incarnation: str
    generation: str
    message_id: str
    wire_seq: None  # Not assigned until an integrated, durable envelope send exists.
    ts: str


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    acquired: bool
    record: ClaimRecord


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ResourceClaims:
    """Single-file, synchronous advisory arbitration; no send integration yet."""

    def __init__(self, claims_dir: Path, resource_root: Path):
        if os.name != "posix":
            raise ClaimError("Resource reservations require a local POSIX filesystem.")
        self._dir = Path(claims_dir).absolute()
        root = Path(resource_root).resolve(strict=True)
        if not root.is_dir():
            raise ClaimError("Resource root must be an existing directory.")
        self._root = root
        self._check_dir()

    def _check_dir(self) -> None:
        try:
            info = self._dir.lstat()
        except FileNotFoundError as error:
            raise ClaimError("Claims directory must already exist with mode 0700.") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ClaimError("Claims directory must be a non-symlink, owner-only 0700 directory.")

    def _resource(self, path: str | Path) -> str:
        requested = Path(path)
        if not requested.is_absolute():
            requested = self._root / requested
        try:
            resolved = requested.resolve(strict=True)
            resolved.relative_to(self._root)
            info = resolved.stat()
        except (FileNotFoundError, ValueError, OSError) as error:
            raise ClaimError(
                "Resource must be an existing file inside the resource root."
            ) from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ClaimError("Only regular singly-linked files can be reserved.")
        return str(resolved)

    def _path_for(self, resource: str) -> Path:
        digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
        return self._dir / digest

    @contextmanager
    def _metadata_lock(self) -> Iterator[None]:
        # This serializes only claim metadata changes, never the work itself.
        # It closes the read-then-unlink/new-reservation race on release.
        self._check_dir()
        path = self._dir / ".metadata.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise UncertainClaim("Metadata lock file is not owner-controlled.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, path: Path, expected_resource: str) -> ClaimRecord:
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or not 1 <= info.st_size <= 4096
            ):
                raise UncertainClaim("Claim file is incomplete or not owner-controlled.")

            def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate claim field")
                    result[key] = value
                return result

            raw = json.loads(path.read_text(), object_pairs_hook=unique_pairs)
            if not isinstance(raw, dict) or set(raw) != set(ClaimRecord.__dataclass_fields__):
                raise ValueError("Invalid claim fields")
            record = ClaimRecord(**raw)
            if (
                record.resource != expected_resource
                or record.wire_seq is not None
                or not all(
                    isinstance(value, str) and bool(value)
                    for value in (
                        record.owner,
                        record.incarnation,
                        record.message_id,
                        record.ts,
                    )
                )
                or len(record.generation) != 32
                or any(char not in "0123456789abcdef" for char in record.generation)
            ):
                raise ValueError("Invalid claim identity")
            return record
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            raise UncertainClaim(
                "Claim record is incomplete or corrupt; manual disposition required."
            ) from error

    def reserve_for_envelope(
        self,
        resource_path: str | Path,
        *,
        owner: str,
        incarnation: str,
        message_id: str,
    ) -> ClaimOutcome:
        """Return one durable winner or synchronous loser, never wait on a claim.

        ``message_id`` is preallocated by the future envelope caller. ``wire_seq``
        remains None: this reservation alone cannot assert a wire append.
        """
        if not all(isinstance(value, str) and value for value in (owner, incarnation, message_id)):
            raise ClaimError("Owner, incarnation, and message id must be non-empty strings.")
        resource = self._resource(resource_path)
        path = self._path_for(resource)
        tombstone = self._dir / f".released-{path.name}"
        with self._metadata_lock():
            if tombstone.exists() or tombstone.is_symlink():
                raise UncertainClaim("An interrupted release requires manual disposition.")
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                return ClaimOutcome(False, self._read(path, resource))
            # If any write/fsync fails, leave the file to block further claims.
            record = ClaimRecord(
                resource=resource,
                owner=owner,
                incarnation=incarnation,
                generation=uuid4().hex,
                message_id=message_id,
                wire_seq=None,
                ts=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(
                        (json.dumps(asdict(record), sort_keys=True) + "\n").encode("utf-8")
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                _fsync_directory(self._dir)
            except BaseException:
                # fdopen owns fd once entered; os.open file remains fail-closed.
                raise
            return ClaimOutcome(True, record)

    def release_for_envelope(
        self,
        resource_path: str | Path,
        *,
        owner: str,
        incarnation: str,
        generation: str,
    ) -> ClaimRecord:
        """Release only the exact current owner generation, without a stale unlink."""
        resource = self._resource(resource_path)
        path = self._path_for(resource)
        tombstone = self._dir / f".released-{path.name}"
        with self._metadata_lock():
            if tombstone.exists() or tombstone.is_symlink():
                raise UncertainClaim("An interrupted release requires manual disposition.")
            try:
                record = self._read(path, resource)
            except UncertainClaim as error:
                if not path.exists() and not path.is_symlink():
                    raise ClaimNotOwned("Resource has no current claim.") from error
                raise
            if (
                record.owner != owner
                or record.incarnation != incarnation
                or record.generation != generation
            ):
                raise ClaimNotOwned("Only the exact current owner generation may release.")
            # The stable metadata lock prevents a second release from unlinking
            # another owner's later reservation. Tombstone protects incomplete
            # releases against a concurrent reservation or process death.
            os.rename(path, tombstone)
            _fsync_directory(self._dir)
            os.unlink(tombstone)
            _fsync_directory(self._dir)
            return record
