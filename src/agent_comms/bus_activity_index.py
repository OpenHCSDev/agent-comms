"""Disposable append checkpoint for channel and sender activity clocks.

The bus JSONL remains authoritative. A replaced or rewritten bus rebuilds this
projection; ordinary appends decode only the new rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

ActivityFields = tuple[str, str, float, bool, bool]
ActivitySnapshot = tuple[dict[str, tuple[float, float]], dict[str, float]]


class BusActivityIndex:
    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_activity_latest.json")

    @staticmethod
    def _tail(stream: Any, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).hexdigest()

    @staticmethod
    def _digest(record: Mapping[str, Any]) -> str:
        payload = {key: value for key, value in record.items() if key != "integrity"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _checkpoint(
        self, revision: tuple[int, int, int, int], stream: Any
    ) -> tuple[ActivitySnapshot, int] | None:
        try:
            saved = json.loads(self.path.read_text())
            if saved.get("integrity") != self._digest(saved):
                return None
            source = saved["source"]
            offset = saved["offset"]
            if (
                saved.get("schema") != 2
                or not isinstance(source, list)
                or len(source) != 4
                or type(offset) is not int
                or offset < 0
                or source[0] != revision[0]
                or offset > revision[1]
                or (offset == revision[1] and source[2:] != list(revision[2:]))
                or saved["tail"] != self._tail(stream, offset)
            ):
                return None
            channels = {
                name: (float(values[0]), float(values[1]))
                for name, values in saved["channels"].items()
                if isinstance(name, str) and isinstance(values, list) and len(values) == 2
            }
            sent = {
                name: float(value) for name, value in saved["sent"].items() if isinstance(name, str)
            }
            if len(channels) != len(saved["channels"]) or len(sent) != len(saved["sent"]):
                return None
            return (channels, sent), offset
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            return None

    def _write(
        self,
        revision: tuple[int, int, int, int],
        stream: Any,
        offset: int,
        snapshot: ActivitySnapshot,
    ) -> None:
        channels, sent = snapshot
        record = {
            "schema": 2,
            "source": list(revision),
            "offset": offset,
            "tail": self._tail(stream, offset),
            "channels": channels,
            "sent": sent,
        }
        record["integrity"] = self._digest(record)
        fd, temporary = tempfile.mkstemp(prefix=".bus-activity-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(record, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def snapshot(
        self,
        revision: tuple[int, int, int, int] | None,
        parse: Callable[[Mapping[str, Any]], ActivityFields],
    ) -> ActivitySnapshot | None:
        if revision is None:
            return {}, {}
        with self.bus_path.open("rb") as stream:
            if revision[1]:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    return None
            checkpoint = self._checkpoint(revision, stream)
            if checkpoint is None:
                channels: dict[str, tuple[float, float]] = {}
                sent: dict[str, float] = {}
                offset = 0
            else:
                (channels, sent), offset = checkpoint
            if offset == revision[1]:
                return channels, sent
            stream.seek(offset)
            while raw := stream.readline():
                if not raw.endswith(b"\n"):
                    return None
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if not isinstance(record, Mapping):
                    raise ValueError("JSONL bus row must be an object")
                sender, target, timestamp, is_user, is_sent = parse(record)
                last_message, last_user = channels.get(target, (0.0, 0.0))
                channels[target] = (
                    max(last_message, timestamp),
                    max(last_user, timestamp) if is_user else last_user,
                )
                if is_sent:
                    sent[sender] = max(sent.get(sender, 0.0), timestamp)
            # A failed disposable checkpoint affects speed, not the wire result.
            with suppress(OSError):
                self._write(revision, stream, revision[1], (channels, sent))
            return channels, sent
