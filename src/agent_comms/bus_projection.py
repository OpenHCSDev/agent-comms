"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Generic, TypeVar

from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity against its bus revision.

    The saved file carries an integrity digest; _read checks it once when the
    bytes are decoded, so an in-memory checkpoint is never re-encoded to check it.
    """

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

    @staticmethod
    def payload_digest(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def signed_payload(self) -> dict:
        payload = FieldCodec.encode(self)
        return {**payload, "integrity": self.payload_digest(payload)}

    def accepts(self, revision: FileRevision, stream: BinaryIO) -> bool:
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
            payload = json.loads(self.path.read_text())
            integrity = payload.pop("integrity")
            if integrity != AppendCheckpoint.payload_digest(payload):
                return None  # Damaged disposable index: rebuilt from the bus.
            return FieldCodec.decode(self.record_type, payload)
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, AttributeError):
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

    def write(self, record: Checkpoint, *, fsync_parent: bool = False) -> None:
        payload = record.signed_payload()
        # A completed historical prefix remains useful after later appends. It
        # may advance the disk index, but never replace a farther valid prefix.
        with _store_lock(self.path):
            try:
                stream = self.bus_path.open("rb")
            except FileNotFoundError:
                return
            with stream:
                current = FileRevision.from_stat(os.fstat(stream.fileno()))
                if not record.accepts(current, stream):
                    return
                saved = self._read()
                if (
                    saved is not None
                    and saved.accepts(current, stream)
                    and saved.offset > record.offset
                ):
                    return
            _atomic_write_text(self.path, json.dumps(payload), fsync_parent=fsync_parent)
