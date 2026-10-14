"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Generic, TypeVar

from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity and integrity policy."""

    source: FileRevision
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

    def accepts(self, revision: FileRevision, stream: BinaryIO) -> bool:
        if self.integrity != self.digest():
            return False
        if self.source.identity != revision.identity or self.offset > revision.size:
            return False
        if self.offset == revision.size and self.source != revision:
            return False
        return self.tail == self.fingerprint(stream, self.offset)


Checkpoint = TypeVar("Checkpoint", bound=AppendCheckpoint)


class BusAppendIndex(Generic[Checkpoint]):
    """One strict record decoder and atomic writer for disposable projections."""

    record_type: type[Checkpoint]

    def __init__(self, bus_path: Path, path: Path):
        self.bus_path, self.path = bus_path, path

    def _read(self) -> Checkpoint | None:
        try:
            return FieldCodec.decode(self.record_type, json.loads(self.path.read_text()))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError):
            return None  # Damaged disposable index: rebuilt from the bus.

    def prepare(self) -> Checkpoint | None:
        """Read a candidate before the bus cut is captured and its lock is taken."""
        return self._read()

    def capture(
        self, prepared: Checkpoint | None, stream: BinaryIO | None,
        revision: FileRevision | None,
    ) -> Checkpoint | None:
        if prepared is None or stream is None or revision is None:
            return None
        if prepared.accepts(revision, stream):
            return prepared
        return None

    def remember(self, record: Checkpoint) -> Checkpoint:
        payload = record.signed_payload()
        return replace(record, integrity=payload["integrity"])

    def write(self, record: Checkpoint, *, fsync_parent: bool = False) -> None:
        retained = self.remember(record)
        payload = retained.signed_payload()
        # A completed historical prefix remains useful after later appends. It
        # may advance the disk index, but never replace a farther valid prefix.
        with _store_lock(self.path):
            try:
                stream = self.bus_path.open("rb")
            except FileNotFoundError:
                return
            with stream:
                current = FileRevision.from_stat(os.fstat(stream.fileno()))
                if not retained.accepts(current, stream):
                    return
                saved = self._read()
                if (
                    saved is not None
                    and saved.accepts(current, stream)
                    and saved.offset > retained.offset
                ):
                    return
            _atomic_write_text(self.path, json.dumps(payload), fsync_parent=fsync_parent)
