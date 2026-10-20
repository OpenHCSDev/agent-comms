"""Activity: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from .bus_projection import NoCheckpoint
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .locked_store import RuntimeFile
from .private_path import FileRevision
from .store_files import (
    _append_jsonl,
    _atomic_write_text,
    _jsonl_records,
    _store_lock,
)
from .thread_identity import OwnerIdentity
from .thread_owned_state import ThreadOwnedState
from .thread_presentation import (
    IdleThreadPresentationStatus,
    ThreadPresentation,
    ThreadPresentationStatus,
    WorkingThreadPresentationStatus,
)

if TYPE_CHECKING:
    from .activity_checkpoint import ActivityCheckpoint
    from .agent_activity import AgentActivity
    from .threads import Thread
    from .turn_phase import TurnPhase


class ThreadOutput(ABC):
    """One phase-to-activity publication for lease and transcript outputs."""

    agents: AgentActivity

    @property
    @abstractmethod
    def activity_thread(self) -> str: ...

    def enter(self, phase: TurnPhase) -> None:
        self.agents.set_activity(self.activity_thread, phase.activity_state,
                                 " ".join(phase.activity_detail.split()))


class ActivityState(Enum):
    """Durable event spellings and their declaration-owned display activity."""

    presentation_status: ThreadPresentationStatus

    def __new__(cls, value: str, presentation_status: ThreadPresentationStatus) -> ActivityState:
        member = object.__new__(cls)
        member._value_ = value
        member.presentation_status = presentation_status
        return member

    IDLE = ("idle", IdleThreadPresentationStatus())
    STARTING = ("starting", WorkingThreadPresentationStatus())
    THINKING = ("thinking", WorkingThreadPresentationStatus())
    WORKING = ("working", WorkingThreadPresentationStatus())
    WAITING_FOR_ROOM = ("waiting for room", WorkingThreadPresentationStatus())
    # Appended once when the thread is permanently deleted. The log stays
    # append-only; the latest-per-thread fold drops the thread on this record.
    DELETED = ("deleted", IdleThreadPresentationStatus())

    @property
    def busy(self) -> bool:
        return self.presentation_status.working

    @property
    def retires_thread(self) -> bool:
        return self is self.DELETED

    def presentation(self, title: str, detail: str) -> ThreadPresentation:
        return self.presentation_status.activity_presentation(title, self.value, detail)


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

    headline: ClassVar[str] = "Inbox unavailable"

    @classmethod
    def from_error(cls, owner: OwnerIdentity, error: Exception) -> Self:
        return cls(owner, type(error).__name__, str(error), recorded_at=time.time())

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


class FailedWakeDiagnostic(UnavailableDrainDiagnostic):
    headline = "Wake failed"


class NativeInputDrainDiagnostic(DrainDiagnostic):
    """A native request failure is not evidence that the inbox cannot be read."""

    headline = "Native request failed"

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
        # Missing diagnostics and those from another process both mean ready.
        if diagnostic is None or diagnostic.owner != owner:
            return ReadyDrainReadiness()
        return diagnostic.readiness()

    @abstractmethod
    def source_diagnostic(self) -> DrainDiagnostic | None: ...

    @abstractmethod
    def presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation: ...

    def require_available(self) -> None:
        pass


@dataclass(frozen=True)
class ReadyDrainReadiness(DrainReadiness):
    def source_diagnostic(self):
        return None

    def presentation(self, ordinary):
        return ordinary


@dataclass(frozen=True)
class DiagnosticDrainReadiness(DrainReadiness):
    """Keep the original observation and its warning without copying either."""

    diagnostic: DrainDiagnostic

    def source_diagnostic(self):
        return self.diagnostic

    def presentation(self, ordinary):
        return ordinary.status.with_attention(self.diagnostic.summary).present_attention(ordinary)


class NativeInputDrainReadiness(DiagnosticDrainReadiness, ReadyDrainReadiness):
    """Retain the failed request warning; new messages keep ordinary availability."""

class UnavailableDrainReadiness(DiagnosticDrainReadiness):
    def require_available(self):
        raise RelationViolationError(self.diagnostic.summary)


@dataclass(frozen=True, slots=True)
class ActivityData:
    """Shared declared activity fields; observations and events own their boundary."""

    detail_limit: ClassVar[int] = 200

    thread: str
    state: ActivityState
    detail: str = ""
    timestamp: float = field(
        default_factory=time.time, metadata={"wire_name": "ts", "wire_required": True}
    )

    @classmethod
    def format_detail(cls, detail: str) -> str:
        """Bound new status text; stored records still use strict construction."""
        return detail if len(detail) <= cls.detail_limit else detail[:cls.detail_limit - 1] + "…"

    def with_status(self, state: ActivityState, detail: str) -> Self:
        """Project current status without changing this activity's provenance."""
        return replace(self, state=state, detail=self.format_detail(detail))

    def presentation(self, title: str) -> ThreadPresentation:
        """Present ordinary activity; acquired observations add their own diagnostics."""
        return self.ordinary_presentation(title)

    def ordinary_presentation(self, title: str) -> ThreadPresentation:
        """Activity alone; the view joins readiness after selecting its source."""
        return self.state.presentation(title, self.detail)

    def __post_init__(self) -> None:
        if not self.thread:
            raise RelationViolationError("Activity thread cannot be empty.")
        self.state.presentation_status.require_activity_detail(self.detail)
        if len(self.detail) > self.detail_limit:
            raise ValueError(f"Activity detail cannot exceed {self.detail_limit} characters.")


@dataclass(frozen=True, slots=True)
class Activity(ActivityData):
    """Original persisted event; its nullable diagnostic is external metadata."""

    diagnostic: DrainDiagnostic | None = field(default=None, metadata={"wire_omit_default": True})

    @classmethod
    def create(cls, thread: str, state: ActivityState, detail: str = "") -> Self:
        """Create a new bounded status, rather than decoding an existing event."""
        return cls(thread, state, cls.format_detail(detail))

    @classmethod
    def absent(cls, thread: str) -> Activity:
        """No event: idle, with no historical clock."""
        return cls(thread, ActivityState.IDLE, timestamp=0.0)

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

    def presentation(self, title: str) -> ThreadPresentation:
        """An acquired activity can also be presented independently of a thread view."""
        return self.readiness.presentation(self.ordinary_presentation(title))


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    """One inseparable source cut and its visible events, including a partial EOF."""

    checkpoint: ActivityCheckpoint | NoCheckpoint = field(default_factory=NoCheckpoint)
    latest: Mapping[str, Activity] = field(default_factory=dict)

@dataclass(slots=True)
class ActivityReadState:
    """What a process has read of one activity log: shared by every reader.

    Each Comms builds its own ActivityLog; the decoded latest events belong
    to the file, so a new reader continues from what this process already
    read instead of decoding the log again. The lock captures and publishes
    cuts; concurrent readers decode without holding it.
    """

    snapshot: ActivitySnapshot = field(default_factory=ActivitySnapshot)
    lock: threading.Lock = field(default_factory=threading.Lock)

    _by_path: ClassVar[dict[str, ActivityReadState]] = {}

    @classmethod
    def for_path(cls, path: Path) -> ActivityReadState:
        return cls._by_path.setdefault(os.path.abspath(path), cls())


class ActivityLog(ThreadOwnedState, RuntimeFile):
    """Persists Activity events as an append-only JSONL log.

    The latest event per thread is its current activity; stale events
    (older than ``stale_after`` seconds) read as idle. Activity is runtime
    state: units' bridges and hooks write it during maintenance.
    """

    filename: ClassVar[str] = "activity.jsonl"

    def __init__(self, store_path: Path, stale_after: float = 120.0):
        self._path = store_path
        from .activity_checkpoint import ActivityCheckpointStore

        self.checkpoint = ActivityCheckpointStore.at(store_path.parent, source_path=store_path)
        self._stale_after = stale_after
        self._read = ActivityReadState.for_path(store_path)

    @contextmanager
    def _writing(self):
        """Prepare outside the source lock; commit only against the exact cut.

        The transition runs once, under the append lock, after its captured
        events are proved current. Missing/invalid checkpoints and long suffixes
        are decoded outside that lock, as is the subsequent projection write.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A writer captures an actual empty source before its first append.
        # NoCheckpoint never qualifies a missing file as a saved source cut.
        self._path.open("ab").close()
        while True:
            snapshot = self._read_snapshot()
            with _store_lock(self._path):
                if not snapshot.checkpoint.current_at(FileRevision.capture(self._path)):
                    continue
                yield snapshot
                after = FileRevision.capture(self._path)
                break
        if not snapshot.checkpoint.current_at(after):
            self._read_snapshot().checkpoint.publish(self.checkpoint)

    def emit(self, activity: Activity) -> None:
        with self._writing():
            _append_jsonl(self._path, FieldCodec.encode(activity))

    def update(self, thread: str, transition: Callable[[Activity], Activity | None]) -> bool:
        """Read and change one thread under the existing activity writer lock.

        The activity owner supplies the transition. Returning no event leaves
        the log unchanged; an append cannot overtake its own comparison.
        """
        with self._writing() as snapshot:
            event = transition(self._observed(
                thread, snapshot.latest, active=True, now=time.time()
            ))
            if event is None:
                return False
            _append_jsonl(self._path, FieldCodec.encode(event))
            return True

    def current(self, thread: str, *, active: bool = False) -> Activity:
        """Latest activity for one thread; idle when stale or unknown."""
        return self._observed(thread, self._read_snapshot().latest, active=active, now=time.time())

    def _observed(self, thread: str, events: Mapping[str, Activity], *,
                  active: bool, now: float) -> Activity:
        if thread not in events:
            # Reading an absent event must never mint an actual-event timestamp.
            return Activity.absent(thread)
        latest = events[thread]
        if (
            latest.diagnostic is None
            and not active
            and now - latest.timestamp > self._stale_after
        ):
            return Activity(thread=thread, state=ActivityState.IDLE, timestamp=latest.timestamp)
        return latest

    def all_current(self, *, active: frozenset[str] = frozenset()) -> dict[str, Activity]:
        """Latest activity for every observed thread."""
        latest = self._read_snapshot().latest
        return self._current_for(latest, latest, active)

    def current_for(self, threads: Iterable[str], *,
                    active: frozenset[str] = frozenset()) -> dict[str, Activity]:
        """Latest activity for the selected thread names, including absent ones."""
        return self._current_for(threads, self._read_snapshot().latest, active)

    def _current_for(self, threads: Iterable[str], events: Mapping[str, Activity],
                     active: frozenset[str]) -> dict[str, Activity]:
        now = time.time()
        return {
            thread: self._observed(thread, events, active=thread in active, now=now)
            for thread in dict.fromkeys(threads)
        }

    def _read_snapshot(self) -> ActivitySnapshot:
        from .activity_checkpoint import ActivityCheckpoint

        read = self._read
        while True:
            with read.lock:
                started_from = read.snapshot
            try:
                with self._path.open("rb") as stream:
                    revision = FileRevision.from_stat(os.fstat(stream.fileno()))
                    if started_from.checkpoint.current_at(revision):
                        return started_from
                    checkpoint = started_from.checkpoint.prepare(self.checkpoint).validated(revision, stream)
                    complete = {}
                    offset = checkpoint.restore_into(complete)
                    latest = complete
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
                    captured = ActivityCheckpoint.capture(stream, revision, complete, offset)
            except FileNotFoundError:
                latest, captured = {}, NoCheckpoint()
            with read.lock:
                if read.snapshot is not started_from:
                    # Another reader may have published a cut taken before this
                    # one. Reacquire the actual current file, not that older cut.
                    continue
                read.snapshot = ActivitySnapshot(captured, latest)
                return read.snapshot

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Append one deletion record per thread with current activity.

        Never rewrite this append-only log: every reader then folds the
        record incrementally instead of decoding the whole file again.
        """
        latest = self._read_snapshot().latest
        for thread in threads:
            if thread.name in latest:
                self.emit(Activity(thread.name, ActivityState.DELETED))

    def rename_thread(self, old_name: str, new_name: str) -> None:
        with self._writing():
            events = (Activity.from_wire(record) for record in _jsonl_records(self._path))
            renamed = (replace(event, thread=new_name) if event.thread == old_name else event
                       for event in events)
            text = "".join(f"{json.dumps(FieldCodec.encode(event))}\n" for event in renamed)
            _atomic_write_text(self._path, text)
