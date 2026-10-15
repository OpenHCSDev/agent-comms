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
from datetime import datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .private_path import FileRevision
from .store_files import (
    _append_jsonl,
    _atomic_write_text,
    _jsonl_records,
    _store_lock,
)
from .thread_identity import OwnerIdentity
from .thread_owned_state import ThreadOwnedState
from .thread_presentation import ThreadPresentation

if TYPE_CHECKING:
    from .agent_activity import AgentActivity
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
    recorded_at: float = field(
        compare=False, metadata={"artifact_exclude": True}
    )

    @property
    @abstractmethod
    def recovery(self) -> str: ...

    @property
    def headline(self) -> str:
        return "Inbox unavailable"

    @property
    def summary(self) -> str:
        return f"{self.headline} · {self.error_type}: {self.reason} · {self.recovery}"

    @projected(view="artifact", name="kind")
    def artifact_kind(self) -> str:
        return FieldCodec.encode(type(self))

    def readiness(self) -> DrainReadiness:
        return UnavailableDrainReadiness(self)


class UnavailableDrainDiagnostic(DrainDiagnostic):
    @property
    def recovery(self) -> str:
        return "Waiting for recovery"


class NativeInputDrainDiagnostic(DrainDiagnostic):
    """A native request failure is not evidence that the inbox cannot be read."""

    @property
    def headline(self) -> str:
        return "Native request failed"

    @property
    def recovery(self) -> str:
        return "See request diagnostics; no automatic resend"

    def readiness(self) -> DrainReadiness:
        return NativeInputDrainReadiness(self)


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
        return diagnostic.readiness()

    @abstractmethod
    def source_diagnostic(self) -> DrainDiagnostic | None: ...

    @abstractmethod
    def presentation(self, ordinary: ThreadPresentation, *, busy: bool) -> ThreadPresentation: ...

    def require_available(self) -> None:
        pass

    async def retire(self, agents: AgentActivity, thread: str) -> DrainReadiness:
        """A healthy readiness has no retained failure to retire."""
        return self

    def preparing_activity(self, activity: ObservedActivity) -> ObservedActivity:
        """An ordinary preparation has no earlier request failure to display."""
        return activity


@dataclass(frozen=True)
class ReadyDrainReadiness(DrainReadiness):
    def source_diagnostic(self):
        return None

    def presentation(self, ordinary, *, busy):
        return ordinary


@dataclass(frozen=True)
class DiagnosticDrainReadiness(DrainReadiness):
    """Keep the original observation and its warning without copying either."""

    diagnostic: DrainDiagnostic

    def source_diagnostic(self):
        return self.diagnostic

    def presentation(self, ordinary, *, busy):
        return ThreadPresentation(ordinary.title, "!", self.diagnostic.summary,
                                  busy=busy, attention=True)

    async def retire(self, agents, thread):
        from .coordinator import Coordination

        await Coordination.run_worker(
            partial(agents.set_drain_diagnostic, thread, self.diagnostic.owner, None)
        )
        return ReadyDrainReadiness()


class NativeInputDrainReadiness(DiagnosticDrainReadiness, ReadyDrainReadiness):
    """Retain the failed request warning; new messages keep ordinary availability."""

    def presentation(self, ordinary, *, busy):
        return ordinary if busy or ordinary.busy else super().presentation(ordinary, busy=busy)

    def preparing_activity(self, activity: ObservedActivity) -> ObservedActivity:
        return replace(activity, readiness=FailedPreparingDrainReadiness(self.diagnostic))


@dataclass(frozen=True)
class FailedPreparingDrainReadiness(NativeInputDrainReadiness):
    """A retained native failure while the next turn has not reached its request."""

    def presentation(self, ordinary, *, busy):
        when = datetime.fromtimestamp(self.diagnostic.recorded_at).astimezone().strftime("%H:%M")
        return replace(
            ordinary,
            marker="!",
            summary=f"Failed {when}",
            busy=busy,
            attention=True,
        )


class UnavailableDrainReadiness(DiagnosticDrainReadiness):
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
        return FieldCodec.decode(cls, data)


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
    read instead of decoding the log again. The lock captures and publishes
    cuts; concurrent readers decode without holding it.
    """

    revision: FileRevision | None = None
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
            # An absent event has no historical clock. Reading it must never
            # mint an actual-event timestamp.
            return Activity(thread, ActivityState.IDLE, timestamp=0.0)
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
        while True:
            with read.lock:
                revision = FileRevision.capture(self._path)
                if revision == read.revision:
                    return read.latest
                started_from = read.revision
                append = (
                    revision is not None
                    and started_from is not None
                    and revision.identity == started_from.identity
                    and revision.size > started_from.size
                )
                checkpoint = self.checkpoint.read() if started_from is None and revision else None
                if checkpoint is not None and not checkpoint.matches(self._path, revision):
                    checkpoint = None
                if checkpoint is not None:
                    complete, offset = dict(checkpoint.latest), checkpoint.offset
                else:
                    complete = read.complete_latest.copy() if append else {}
                    offset = read.offset if append else 0
            original_offset = offset
            latest = complete
            if revision is not None:
                try:
                    with self._path.open("rb") as stream:
                        if not revision.opened_by(stream):
                            continue
                        stream.seek(offset)
                        while stream.tell() < revision.size:
                            raw = stream.readline(revision.size - stream.tell())
                            if not raw:
                                break
                            terminated = raw.endswith((b"\n", b"\r"))
                            if line := raw.strip():
                                try:
                                    record = json.loads(line)
                                except (json.JSONDecodeError, UnicodeDecodeError):
                                    if not terminated:
                                        break
                                    raise
                                if not isinstance(record, Mapping):
                                    raise ValueError(
                                        f"JSONL record in {self._path} must be an object."
                                    )
                                event = Activity.from_wire(record)
                                if terminated:
                                    event.fold_into(complete)
                                else:
                                    # The writer repairs an interrupted final row before its
                                    # next append (store_files.py:_append_jsonl).
                                    latest = dict(complete)
                                    event.fold_into(latest)
                            if terminated:
                                offset = stream.tell()
                except FileNotFoundError:
                    continue
            with read.lock:
                if read.revision != started_from:
                    return read.latest
                read.complete_latest, read.offset = complete, offset
                read.latest, read.revision = latest, revision
            if revision is not None and (checkpoint is None or offset != original_offset):
                # The log remains authoritative if its disposable read
                # projection cannot be persisted.
                with suppress(OSError):
                    from .activity_checkpoint import ActivityCheckpoint

                    captured = ActivityCheckpoint.capture(self._path, revision, complete, offset)
                    self.checkpoint.replace(captured)
            return latest

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
