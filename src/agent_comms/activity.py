"""Activity: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import threading
import time
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .store_files import (
    _append_jsonl,
    _atomic_write_text,
    _jsonl_records,
    _store_lock,
    file_revision,
)
from .thread_identity import OwnerIdentity
from .thread_owned_state import ThreadOwnedState
from .thread_presentation import ThreadPresentation

if TYPE_CHECKING:
    from .presentation import MessageNotification
    from .threads import Thread


class ActivityState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    # Appended once when the thread is permanently deleted. The log stays
    # append-only; the latest-per-thread fold drops the thread on this record.
    DELETED = "deleted"

    @property
    def busy(self) -> bool:
        return self in (self.THINKING, self.WORKING)

    @property
    def retires_thread(self) -> bool:
        return self is self.DELETED

    def presentation(self, title: str, detail: str) -> ThreadPresentation:
        if not self.busy:
            return ThreadPresentation(title, "✓", "Ready")
        summary = self.value.title()
        if detail := " ".join(detail.split()):
            summary += f" · {detail}"
        return ThreadPresentation(title, "⌛", summary, busy=True)


@dataclass(frozen=True)
class DrainDiagnostic(DeclaredFamily, affix="Diagnostic"):
    """An owner's observed failure, never delivery or retry authority."""

    owner: OwnerIdentity
    error_type: str
    reason: str

    @property
    @abstractmethod
    def recovery(self) -> str: ...

    @property
    def summary(self) -> str:
        return f"Inbox unavailable · {self.error_type}: {self.reason} · {self.recovery}"


class UnavailableDrainDiagnostic(DrainDiagnostic):
    @property
    def recovery(self) -> str:
        return "Waiting for recovery"


class StoppedDrainDiagnostic(DrainDiagnostic):
    @property
    def recovery(self) -> str:
        return "Drain stopped; fix the error and restart the owner"


class DrainReadiness(DeclaredFamily, affix="DrainReadiness"):
    """Owner-fenced interpretation of the original activity event, not a store."""

    @classmethod
    def acquire(cls, diagnostic: DrainDiagnostic | None, owner: OwnerIdentity) -> DrainReadiness:
        # Nullable metadata is classified only at the original source boundary.
        if diagnostic is None or diagnostic.owner != owner:
            return ReadyDrainReadiness()
        return UnavailableDrainReadiness(diagnostic)

    @abstractmethod
    def source_diagnostic(self) -> DrainDiagnostic | None: ...

    @abstractmethod
    def pending_notification(self, notification: MessageNotification) -> MessageNotification: ...

    @abstractmethod
    def presentation(self, ordinary: ThreadPresentation, *, busy: bool) -> ThreadPresentation: ...

    def require_available(self) -> None:
        pass


@dataclass(frozen=True)
class ReadyDrainReadiness(DrainReadiness):
    def source_diagnostic(self):
        return None

    def pending_notification(self, notification: MessageNotification) -> MessageNotification:
        return notification

    def presentation(self, ordinary, *, busy):
        return ordinary


@dataclass(frozen=True)
class UnavailableDrainReadiness(DrainReadiness):
    diagnostic: DrainDiagnostic

    def source_diagnostic(self):
        return self.diagnostic

    def pending_notification(self, notification: MessageNotification) -> MessageNotification:
        return replace(notification, state="Waiting for recovery",
            detail=f"{self.diagnostic.summary}. This message is saved and has not started.",
            priority=4, busy=False)

    def presentation(self, ordinary, *, busy):
        return ThreadPresentation(ordinary.title, "!", self.diagnostic.summary,
                                  busy=busy, attention=True)

    def require_available(self):
        raise RelationViolationError(self.diagnostic.summary)


@dataclass(frozen=True, slots=True)
class ActivityData:
    """Shared declared activity fields; observations and events own their boundary."""

    thread: str
    state: ActivityState
    detail: str = ""
    timestamp: float = field(
        default_factory=time.time, metadata={"wire_name": "ts", "wire_required": True}
    )

    def presentation(self, title: str) -> ThreadPresentation:
        return self.state.presentation(title, self.detail)

    def __post_init__(self) -> None:
        if not self.thread:
            raise RelationViolationError("Activity thread cannot be empty.")
        if self.detail and not self.state.busy:
            raise RelationViolationError("Idle activity cannot carry a detail.")
        if len(self.detail) > 200:
            raise ValueError("Activity detail cannot exceed 200 characters.")


@dataclass(frozen=True, slots=True)
class Activity(ActivityData):
    """Original persisted event; its nullable diagnostic is external metadata."""

    diagnostic: DrainDiagnostic | None = field(default=None, metadata={"wire_omit_default": True})

    def fold_into(self, latest: dict[str, Activity]) -> None:
        """Apply this event to the latest-per-thread map; a deletion leaves no entry."""
        if self.state.retires_thread:
            latest.pop(self.thread, None)
        else:
            latest[self.thread] = self

    @classmethod
    def from_wire(cls, data: Mapping) -> Activity:
        # Historical event timestamps may be absent; they remain unknown.
        return FieldCodec.decode(cls, {"ts": 0.0, **data})


@dataclass(frozen=True, slots=True)
class ObservedActivity(ActivityData):
    """Acquired activity has one required readiness, no nullable diagnostic copy."""

    readiness: DrainReadiness = field(kw_only=True)

    @classmethod
    def acquire(cls, event: Activity, owner: OwnerIdentity) -> ObservedActivity:
        return cls(event.thread, event.state, event.detail, event.timestamp,
                   readiness=DrainReadiness.acquire(event.diagnostic, owner))

    def source_event(self) -> Activity:
        """Write through the original event boundary; never persist a second state."""
        return Activity(self.thread, self.state, self.detail, self.timestamp,
                        self.readiness.source_diagnostic())

    def presentation(self, title):
        return self.readiness.presentation(super().presentation(title), busy=self.state.busy)


@dataclass(slots=True)
class ActivityReadState:
    """What a process has read of one activity log: shared by every reader.

    Each Comms builds its own ActivityLog; the decoded latest events belong
    to the file, so a new reader continues from what this process already
    read instead of decoding the log again. The lock makes one thread catch
    up while concurrent readers wait for its result.
    """

    revision: tuple | None = None
    latest: dict[str, Activity] = field(default_factory=dict)
    complete_latest: dict[str, Activity] = field(default_factory=dict)
    offset: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    _by_path: ClassVar[dict[str, ActivityReadState]] = {}

    @classmethod
    def for_path(cls, path: Path) -> ActivityReadState:
        return cls._by_path.setdefault(os.path.abspath(path), cls())


class ActivityLog(ThreadOwnedState):
    """Persists Activity events as an append-only JSONL log.

    The latest event per thread is its current activity; stale events
    (older than ``stale_after`` seconds) read as idle.
    """

    def __init__(self, store_path: Path, stale_after: float = 120.0):
        self._path = store_path
        from .activity_checkpoint import ActivityCheckpointStore

        self.checkpoint = ActivityCheckpointStore(
            store_path.with_name(ActivityCheckpointStore.filename)
        )
        self._stale_after = stale_after
        self._read = ActivityReadState.for_path(store_path)

    def emit(self, activity: Activity) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self._path):
            _append_jsonl(self._path, FieldCodec.encode(activity))

    def current(self, thread: str, *, active: bool = False) -> Activity:
        """Latest activity for one thread; idle when stale or unknown."""
        return self._observed(thread, self._latest_events(), active=active, now=time.time())

    def _observed(self, thread: str, events: Mapping[str, Activity], *,
                  active: bool, now: float) -> Activity:
        if thread not in events:
            # The existing decoder owns an unknown historical clock. Reading
            # an absent event must never mint an actual-event timestamp.
            return Activity.from_wire({"thread": thread, "state": ActivityState.IDLE.value})
        latest = events[thread]
        if (
            latest.diagnostic is None
            and not active
            and now - latest.timestamp > self._stale_after
        ):
            return Activity(thread=thread, state=ActivityState.IDLE, timestamp=latest.timestamp)
        return latest

    def all_current(self, *, active: frozenset[str] = frozenset(),
                    threads: Iterable[str] = ()) -> dict[str, Activity]:
        """Latest activity per thread (idle included for known threads)."""
        result = self._latest_events()
        now = time.time()
        return {
            thread: self._observed(thread, result, active=thread in active, now=now)
            for thread in dict.fromkeys((*result, *threads))
        }

    def _latest_events(self) -> Mapping[str, Activity]:
        read = self._read
        with read.lock, _store_lock(self._path):
            revision = file_revision(self._path)
            if revision == read.revision:
                return read.latest
            append = (
                revision is not None
                and read.revision is not None
                and revision[0] == read.revision[0]
                and revision[1] > read.revision[1]
            )
            # Publish a new map: other threads may still be iterating the old
            # snapshot after the store lock has been released.
            checkpoint = (
                self.checkpoint.read() if read.revision is None and revision is not None else None
            )
            if checkpoint is not None and not checkpoint.matches(self._path, revision):
                checkpoint = None
            if checkpoint is not None:
                complete, offset = checkpoint.latest, checkpoint.offset
            else:
                complete = read.complete_latest.copy() if append else {}
                offset = read.offset if append else 0
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
                                event.fold_into(complete)
                            else:
                                # Preserve the existing reader's valid-EOF
                                # behavior, but retry this tail on the next append.
                                latest = dict(complete)
                                event.fold_into(latest)
                        if terminated:
                            offset = stream.tell()
            read.complete_latest, read.offset = complete, offset
            read.latest, read.revision = latest, revision
            if revision is not None and (checkpoint is None or offset != original_offset):
                # The log remains authoritative if its disposable read
                # projection cannot be persisted.
                with suppress(OSError):
                    from .activity_checkpoint import ActivityCheckpoint

                    captured = ActivityCheckpoint.capture(self._path, revision, complete, offset)
                    self.checkpoint.replace(captured)
            return read.latest

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Append one deletion record per thread with current activity.

        Never rewrite this append-only log: every reader then folds the
        record incrementally instead of decoding the whole file again.
        """
        latest = self._latest_events()
        for thread in threads:
            if thread.name in latest:
                self.emit(Activity(thread.name, ActivityState.DELETED))

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
