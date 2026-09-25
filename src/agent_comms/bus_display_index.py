"""Disposable, scope-bound append checkpoint for a viewer's sidebar metrics.

The bus JSONL owns the messages. A changed display predicate or a damaged
checkpoint rebuilds from the bus; an ordinary append reads only its new rows.
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

DisplayMetrics = tuple[dict[str, tuple[float, float]], dict[str, int]]


class BusDisplayIndex:
    def __init__(self, bus_path: Path, viewer: str):
        self.bus_path = bus_path
        digest = hashlib.sha256(viewer.encode("utf-8")).hexdigest()
        self.path = bus_path.with_name(f"bus_display_{digest}.json")

    @staticmethod
    def _digest(record: Mapping[str, Any]) -> str:
        payload = {key: value for key, value in record.items() if key != "integrity"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _tail(stream: Any, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).hexdigest()

    def _load(
        self,
        stream: Any,
        revision: tuple[int, int, int, int],
        semantics: list[Any],
        initial: DisplayMetrics,
    ) -> tuple[DisplayMetrics, int] | None:
        try:
            saved = json.loads(self.path.read_text())
            source = saved["source"]
            offset = saved["offset"]
            if (
                saved.get("schema") != 1
                or saved.get("integrity") != self._digest(saved)
                or saved.get("semantics") != semantics
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
            activity = saved["activity"]
            counts = saved["counts"]
            if (
                not isinstance(activity, dict)
                or not isinstance(counts, dict)
                or activity.keys() != initial[0].keys()
                or counts.keys() != initial[1].keys()
            ):
                return None
            parsed_activity = {
                name: (float(values[0]), float(values[1]))
                for name, values in activity.items()
                if isinstance(values, list)
                and len(values) == 2
                and all(type(value) in (int, float) for value in values)
            }
            parsed_counts = {
                name: value for name, value in counts.items() if type(value) is int and value >= 0
            }
            if len(parsed_activity) != len(activity) or len(parsed_counts) != len(counts):
                return None
            return (parsed_activity, parsed_counts), offset
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            return None

    def _write(
        self,
        revision: tuple[int, int, int, int],
        stream: Any,
        semantics: list[Any],
        metrics: DisplayMetrics,
    ) -> None:
        record = {
            "schema": 1,
            "source": list(revision),
            "offset": revision[1],
            "tail": self._tail(stream, revision[1]),
            "semantics": semantics,
            "activity": metrics[0],
            "counts": metrics[1],
        }
        record["integrity"] = self._digest(record)
        fd, temporary = tempfile.mkstemp(prefix=".bus-display-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(record, output)
                output.flush()
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def snapshot(
        self,
        revision: tuple[int, int, int, int] | None,
        semantics: list[Any],
        initial: DisplayMetrics,
        apply: Callable[[Mapping[str, Any], DisplayMetrics], None],
    ) -> DisplayMetrics | None:
        if revision is None:
            # The caller may have opened a newer bus boundary after its first
            # revision check. Its captured records then own this snapshot.
            return None
        try:
            stream = self.bus_path.open("rb")
        except FileNotFoundError:
            return None
        with stream:
            stat = os.fstat(stream.fileno())
            if stat.st_ino != revision[0] or stat.st_size < revision[1]:
                return None
            if revision[1]:
                stream.seek(revision[1] - 1)
                if stream.read(1) != b"\n":
                    return None
            checkpoint = self._load(stream, revision, semantics, initial)
            if checkpoint is None:
                metrics = dict(initial[0]), dict(initial[1])
                offset = 0
            else:
                metrics, offset = checkpoint
            stream.seek(offset)
            while stream.tell() < revision[1]:
                raw = stream.readline()
                if stream.tell() > revision[1] or not raw.endswith(b"\n"):
                    return None
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if not isinstance(record, Mapping):
                    raise ValueError("JSONL bus row must be an object")
                apply(record, metrics)
            if offset != revision[1]:
                with suppress(OSError):
                    self._write(revision, stream, semantics, metrics)
            return metrics
