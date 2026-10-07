"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .field_codec import FieldCodec
from .store_files import _atomic_write_text


@dataclass(frozen=True)
class BusFileRevision:
    """The existing file_revision observation, with its fields declared once."""

    inode: int
    size: int
    modified: int
    changed: int

    @classmethod
    def capture(cls, stream: BinaryIO) -> BusFileRevision:
        opened = os.fstat(stream.fileno())
        return cls(opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)

    def opened_by(self, stream: BinaryIO) -> bool:
        opened = os.fstat(stream.fileno())
        if opened.st_ino != self.inode or opened.st_size < self.size:
            return False
        if opened.st_size == self.size:
            return self == type(self)(opened.st_ino, opened.st_size,
                                      opened.st_mtime_ns, opened.st_ctime_ns)
        return True


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity and integrity policy."""

    source: BusFileRevision
    offset: int
    tail: str
    integrity: str = field(default="", kw_only=True)

    def __post_init__(self) -> None:
        if not 0 <= self.offset <= self.source.size:
            raise ValueError("Projection offset exceeds its original source")

    @staticmethod
    def fingerprint(stream: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).hexdigest()

    @staticmethod
    def payload_digest(payload: dict) -> str:
        unsigned = {key: value for key, value in payload.items() if key != "integrity"}
        raw = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def digest(self) -> str:
        return self.payload_digest(FieldCodec.encode(self))

    def signed_payload(self) -> dict:
        """Encode the projection once for both integrity and atomic persistence."""
        payload = FieldCodec.encode(self)
        payload["integrity"] = self.payload_digest(payload)
        return payload

    def accepts(self, revision: BusFileRevision, stream: BinaryIO) -> bool:
        if self.integrity != self.digest():
            return False
        if self.source.inode != revision.inode or self.offset > revision.size:
            return False
        if self.offset == revision.size and self.source != revision:
            return False
        return self.tail == self.fingerprint(stream, self.offset)


class BusAppendIndex:
    """One strict record decoder and atomic writer for disposable projections."""

    record_type: type[AppendCheckpoint]

    def __init__(self, bus_path: Path, path: Path):
        self.bus_path, self.path = bus_path, path

    def checkpoint(self, stream: BinaryIO, revision: BusFileRevision):
        try:
            record = FieldCodec.decode(self.record_type, json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return record if record.accepts(revision, stream) else None

    def write(self, record: AppendCheckpoint, *, fsync_parent: bool = False) -> None:
        _atomic_write_text(self.path, json.dumps(record.signed_payload()),
                           fsync_parent=fsync_parent)
