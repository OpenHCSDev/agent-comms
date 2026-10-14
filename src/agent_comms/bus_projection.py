"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Generic, Self, TypeVar

from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock

_LOG = logging.getLogger(__name__)
_SIGNED_PREFIX = '{"payload":'
_SIGNED_SUFFIX = re.compile(r',"integrity":"([0-9a-f]{64})"}\Z')


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity and source-boundary policy."""

    source: FileRevision
    offset: int
    tail: str

    def __post_init__(self) -> None:
        if not 0 <= self.offset <= self.source.size:
            raise ValueError("Projection offset exceeds its original source")

    @staticmethod
    def fingerprint(stream: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).hexdigest()

    def signed_payload(self) -> StoredCheckpoint[Self]:
        """Encode once; sign the exact payload text the reader will verify."""
        payload = FieldCodec.encode(self)
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(text.encode()).hexdigest()
        return StoredCheckpoint(
            self, f'{_SIGNED_PREFIX}{text},"integrity":"{digest}"}}'
        )

    def accepts(self, revision: FileRevision, stream: BinaryIO) -> bool:
        if self.source.identity != revision.identity or self.offset > revision.size:
            return False
        if self.offset == revision.size and self.source != revision:
            return False
        return self.tail == self.fingerprint(stream, self.offset)


Checkpoint = TypeVar("Checkpoint", bound=AppendCheckpoint)


@dataclass(frozen=True)
class StoredCheckpoint(Generic[Checkpoint]):
    """A checkpoint whose exact stored payload text passed its digest."""

    record: Checkpoint
    text: str
    revision: FileRevision | None = None

    @classmethod
    def decode(
        cls, record_type: type[Checkpoint], text: str, revision: FileRevision
    ) -> StoredCheckpoint[Checkpoint]:
        if not text.startswith(_SIGNED_PREFIX):
            raise ValueError("Projection index lacks its signed payload")
        payload, end = json.JSONDecoder().raw_decode(text, len(_SIGNED_PREFIX))
        match = _SIGNED_SUFFIX.fullmatch(text[end:])
        if match is None:
            raise ValueError("Projection index has an invalid integrity envelope")
        payload_text = text[len(_SIGNED_PREFIX):end]
        digest = hashlib.sha256(payload_text.encode()).hexdigest()
        if not hmac.compare_digest(match.group(1), digest):
            raise ValueError("Projection index payload digest changed")
        return cls(FieldCodec.decode(record_type, payload), text, revision)


@dataclass(frozen=True)
class DamagedIndex:
    """A disposable projection exists but cannot be trusted or decoded."""

    revision: FileRevision
    reason: str

    def rebuild(self, owner: BusAppendIndex) -> None:
        owner.report_damage(self)


PreparedCheckpoint = StoredCheckpoint[Checkpoint] | DamagedIndex | None


class BusAppendIndex(Generic[Checkpoint]):
    """One strict record decoder and atomic writer for disposable projections."""

    record_type: type[Checkpoint]

    def __init__(self, bus_path: Path, path: Path):
        self.bus_path, self.path = bus_path, path
        self._prepared_revision: FileRevision | None = None
        self._prepared: PreparedCheckpoint = None
        self._prepared_known = False
        self._reported_damage: FileRevision | None = None

    def _read(self) -> PreparedCheckpoint:
        try:
            with self.path.open() as stream:
                text = stream.read()
                revision = FileRevision.from_stat(os.fstat(stream.fileno()))
        except FileNotFoundError:
            return None
        try:
            return StoredCheckpoint.decode(self.record_type, text, revision)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            return DamagedIndex(revision, str(error))

    def report_damage(self, damaged: DamagedIndex) -> None:
        """Name one damaged revision once; its caller rebuilds from the bus."""
        if self._reported_damage != damaged.revision:
            _LOG.warning("Rebuilding damaged disposable bus index %s: %s", self.path, damaged.reason)
            self._reported_damage = damaged.revision

    def prepare(self) -> PreparedCheckpoint:
        """Read a candidate before the bus cut; retain its decoded current revision."""
        revision = FileRevision.capture(self.path)
        if self._prepared_known and revision == self._prepared_revision:
            return self._prepared
        prepared = self._read()
        self._prepared_revision = None if prepared is None else prepared.revision
        self._prepared = prepared
        self._prepared_known = True
        return prepared

    def capture(
        self, prepared: PreparedCheckpoint, stream: BinaryIO | None,
        revision: FileRevision | None,
    ) -> Checkpoint | None:
        if isinstance(prepared, DamagedIndex):
            prepared.rebuild(self)
            return None
        if prepared is None or stream is None or revision is None:
            return None
        if prepared.record.accepts(revision, stream):
            return prepared.record
        return None

    def remember(self, record: Checkpoint) -> StoredCheckpoint[Checkpoint]:
        return record.signed_payload()

    def write(
        self, value: Checkpoint | StoredCheckpoint[Checkpoint], *, fsync_parent: bool = False
    ) -> StoredCheckpoint[Checkpoint]:
        retained = value if isinstance(value, StoredCheckpoint) else self.remember(value)
        record = retained.record
        # A completed historical prefix remains useful after later appends. It
        # may advance the disk index, but never replace a farther valid prefix.
        with _store_lock(self.path):
            try:
                stream = self.bus_path.open("rb")
            except FileNotFoundError:
                return retained
            with stream:
                current = FileRevision.from_stat(os.fstat(stream.fileno()))
                if not record.accepts(current, stream):
                    return retained
                saved = self._read()
                if isinstance(saved, DamagedIndex):
                    saved.rebuild(self)
                    saved = None
                if (
                    saved is not None
                    and saved.record.accepts(current, stream)
                    and saved.record.offset > record.offset
                ):
                    return retained
            _atomic_write_text(self.path, retained.text, fsync_parent=fsync_parent)
        retained = replace(retained, revision=FileRevision.capture(self.path))
        self._prepared_revision = retained.revision
        self._prepared = retained
        self._prepared_known = True
        self._reported_damage = None
        return retained
