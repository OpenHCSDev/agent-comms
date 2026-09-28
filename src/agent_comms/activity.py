"""Activity: declaration and persistence owners."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .errors import RelationViolationError
from .store_files import (
    _append_jsonl,
    _atomic_write_text,
    _jsonl_records,
    _store_lock,
    file_revision,
)
from .thread_presentation import ThreadPresentation


class ActivityState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"

    @property
    def busy(self) -> bool:
        return self is not self.IDLE

    def presentation(self, title: str, detail: str) -> ThreadPresentation:
        if not self.busy:
            return ThreadPresentation(title, "✓", "Ready")
        summary = self.value.title()
        if detail := " ".join(detail.split()):
            summary += f" · {detail}"
        return ThreadPresentation(title, "⌛", summary, busy=True)


@dataclass(frozen=True, slots=True)
class Activity:
    """Declares one thread's current activity (what it is doing right now).

    Emitted by participants and agent turns so other clients can show live
    feedback. Appended to ``activity.jsonl``; the latest event per thread is
    the thread's state. Empty detail is valid (plain thinking).
    """

    thread: str
    state: ActivityState
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.thread:
            raise RelationViolationError("Activity thread cannot be empty.")
        if self.detail and not self.state.busy:
            raise RelationViolationError("Idle activity cannot carry a detail.")
        if len(self.detail) > 200:
            raise ValueError("Activity detail cannot exceed 200 characters.")

    def to_wire(self) -> dict:
        return {
            "thread": self.thread,
            "state": self.state.value,
            "detail": self.detail,
            "ts": self.timestamp,
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> Activity:
        return cls(
            thread=data["thread"],
            state=ActivityState(data["state"]),
            detail=data.get("detail", ""),
            timestamp=data.get("ts", 0.0),
        )


class ActivityLog:
    """Persists Activity events as an append-only JSONL log.

    The latest event per thread is its current activity; stale events
    (older than ``stale_after`` seconds) read as idle.
    """

    def __init__(self, store_path: Path, stale_after: float = 120.0):
        self._path = store_path
        self._checkpoint_path = store_path.with_name("activity_latest.json")
        self._stale_after = stale_after
        self._revision: tuple | None = None
        self._latest: dict[str, Activity] = {}
        self._complete_latest: dict[str, Activity] = self._latest
        self._offset = 0

    def emit(self, activity: Activity) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            _append_jsonl(self._path, activity.to_wire())

    def current(self, thread: str, *, active: bool = False) -> Activity:
        """Latest activity for one thread; idle when stale or unknown."""
        latest = self._latest_events().get(thread)
        if latest is None:
            return Activity(thread=thread, state=ActivityState.IDLE)
        if not active and time.time() - latest.timestamp > self._stale_after:
            return Activity(thread=thread, state=ActivityState.IDLE, timestamp=latest.timestamp)
        return latest

    def all_current(self, *, active: frozenset[str] = frozenset()) -> dict[str, Activity]:
        """Latest activity per thread (idle included for known threads)."""
        result = self._latest_events()
        now = time.time()
        return {
            thread: (
                activity
                if thread in active or now - activity.timestamp <= self._stale_after
                else Activity(thread=thread, state=ActivityState.IDLE, timestamp=activity.timestamp)
            )
            for thread, activity in result.items()
        }

    def _latest_events(self) -> Mapping[str, Activity]:
        with _store_lock(self._path):
            revision = file_revision(self._path)
            if revision == self._revision:
                return self._latest
            append = (
                revision is not None
                and self._revision is not None
                and revision[0] == self._revision[0]
                and revision[1] > self._revision[1]
            )
            # Publish a new map: other threads may still be iterating the old
            # snapshot after the store lock has been released.
            checkpoint = (
                self._read_checkpoint_unlocked(revision)
                if self._revision is None and revision is not None
                else None
            )
            if checkpoint is not None:
                complete, offset = checkpoint
            else:
                complete = self._complete_latest.copy() if append else {}
                offset = self._offset if append else 0
            original_offset = offset
            latest = complete
            if revision is not None:
                with self._path.open("rb") as stream:
                    stream.seek(offset)
                    while raw := stream.readline():
                        terminated = raw.endswith((b"\n", b"\r"))
                        if line := raw.strip():
                            try:
                                record = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                if not terminated:
                                    break
                                raise
                            if not isinstance(record, Mapping):
                                raise ValueError(f"JSONL record in {self._path} must be an object.")
                            event = Activity.from_wire(record)
                            if terminated:
                                complete[event.thread] = event
                            else:
                                # Preserve the existing reader's valid-EOF
                                # behavior, but retry this tail on the next append.
                                latest = {**complete, event.thread: event}
                        if terminated:
                            offset = stream.tell()
            self._complete_latest, self._offset = complete, offset
            self._latest, self._revision = latest, revision
            if revision is not None and (checkpoint is None or offset != original_offset):
                # The log remains authoritative if its disposable read
                # projection cannot be persisted.
                with suppress(OSError):
                    self._write_checkpoint_unlocked(revision, complete, offset)
            return self._latest

    def _read_checkpoint_unlocked(
        self, revision: tuple[int, int, int, int]
    ) -> tuple[dict[str, Activity], int] | None:
        try:
            stored = json.loads(self._checkpoint_path.read_text())
            source = stored["source"]
            offset = stored["offset"]
            if (
                stored.get("schema") != 1
                or not isinstance(source, list)
                or len(source) != 4
                or type(offset) is not int
                or offset < 0
                or source[0] != revision[0]
                or offset > revision[1]
                or (source[1] == revision[1] and source[2:] != list(revision[2:]))
            ):
                return None
            with self._path.open("rb") as stream:
                start = max(0, offset - 4096)
                stream.seek(start)
                if hashlib.sha256(stream.read(offset - start)).hexdigest() != stored["tail"]:
                    return None
            rows = stored["latest"]
            if not isinstance(rows, dict):
                return None
            latest = {name: Activity.from_wire(row) for name, row in rows.items()}
            if any(name != activity.thread for name, activity in latest.items()):
                return None
            return latest, offset
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _write_checkpoint_unlocked(
        self,
        revision: tuple[int, int, int, int],
        complete: Mapping[str, Activity],
        offset: int,
    ) -> None:
        with self._path.open("rb") as stream:
            start = max(0, offset - 4096)
            stream.seek(start)
            tail = hashlib.sha256(stream.read(offset - start)).hexdigest()
        _atomic_write_text(
            self._checkpoint_path,
            json.dumps(
                {
                    "schema": 1,
                    "source": list(revision),
                    "offset": offset,
                    "tail": tail,
                    "latest": {name: activity.to_wire() for name, activity in complete.items()},
                }
            ),
        )

    def _load(self) -> list[Activity]:
        with _store_lock(self._path):
            return [Activity.from_wire(record) for record in _jsonl_records(self._path)]

    def remove_thread(self, thread: str) -> int:
        """Remove all persisted activity for one thread."""
        with _store_lock(self._path):
            records = [dict(record) for record in _jsonl_records(self._path)]
            retained = [record for record in records if record.get("thread") != thread]
            _atomic_write_text(
                self._path,
                "".join(f"{json.dumps(record)}\n" for record in retained),
            )
        return len(records) - len(retained)

    def rename_thread(self, old_name: str, new_name: str) -> None:
        with _store_lock(self._path):
            records = [dict(record) for record in _jsonl_records(self._path)]
            for record in records:
                if record.get("thread") == old_name:
                    record["thread"] = new_name
            _atomic_write_text(
                self._path,
                "".join(f"{json.dumps(record)}\n" for record in records),
            )
