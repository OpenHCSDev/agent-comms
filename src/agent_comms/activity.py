"""Activity: declaration and persistence owners."""

from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, Annotated


from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    NullMember,
    projected,
    ArtifactExclude,
    WireName,
    WireOmitDefault,
    WireRequired,
)
from agent_comms.typed_table import StoreRows, Column, DerivedStore, TypedTable
from .thread_identity import OwnerIdentity
from .thread_owned_state import ThreadOwnedState
from .thread_presentation import (
    IdleThreadPresentationStatus,
    ThreadPresentation,
    ThreadPresentationStatus,
    WorkingThreadPresentationStatus,
)

if TYPE_CHECKING:
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
    # Permanent deletion removes the thread's current activity row.
    DELETED = ("deleted", IdleThreadPresentationStatus())

    @property
    def busy(self) -> bool:
        return self.presentation_status.working

    @property
    def retires_thread(self) -> bool:
        return self is self.DELETED

    def presentation(self, title: str, detail: str) -> ThreadPresentation:
        return self.presentation_status.activity_presentation(title, self.value, detail)


class DrainObservation(ABC):
    """What an activity event recorded about its owner's drain: a failure, or none."""

    # A recorded failure keeps its event current instead of aging it to idle.
    holds_event_current: ClassVar[bool]

    @abstractmethod
    def readiness(self) -> DrainReadiness: ...

    @abstractmethod
    def recorded_by(self, owner: OwnerIdentity) -> bool: ...

    @abstractmethod
    def recorded_before(self, moment: float) -> bool: ...

    @abstractmethod
    def same_occurrence(self, other: DrainObservation) -> bool: ...

    @abstractmethod
    def occurrence_of(self, other: DrainDiagnostic) -> bool: ...

    def readiness_for(self, owner: OwnerIdentity) -> DrainReadiness:
        # A failure recorded by another process means ready.
        return self.readiness() if self.recorded_by(owner) else ReadyDrainReadiness()


@dataclass(frozen=True)
class DrainDiagnostic(DeclaredFamily, DrainObservation, affix="Diagnostic"):
    """An owner's observed failure, never delivery or retry authority."""

    owner: OwnerIdentity
    error_type: str
    reason: str
    recorded_at: Annotated[float, ArtifactExclude()] = field(compare=False)

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

    holds_event_current = True

    def recorded_by(self, owner: OwnerIdentity) -> bool:
        return self.owner == owner

    def recorded_before(self, moment: float) -> bool:
        return self.recorded_at < moment

    def same_occurrence(self, other: DrainObservation) -> bool:
        return other.occurrence_of(self)

    def occurrence_of(self, other: DrainDiagnostic) -> bool:
        return self == other and self.recorded_at == other.recorded_at


@dataclass(frozen=True, slots=True)
class NoDrainDiagnostic(NullMember, DrainObservation):
    """The owner has recorded no drain failure for this activity."""

    holds_event_current = False

    def readiness(self) -> DrainReadiness:
        return ReadyDrainReadiness()

    def recorded_by(self, owner: OwnerIdentity) -> bool:
        return False

    def recorded_before(self, moment: float) -> bool:
        return False

    def same_occurrence(self, other: DrainObservation) -> bool:
        return False

    def occurrence_of(self, other: DrainDiagnostic) -> bool:
        return False


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

    @abstractmethod
    def source_diagnostic(self) -> DrainDiagnostic | NoDrainDiagnostic: ...

    @abstractmethod
    def presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation: ...

    def require_available(self) -> None:
        pass

    def unavailable_since(self) -> tuple[float, ...]:
        """When nothing could reach this owner's inbox; empty while it is reachable."""
        return ()


@dataclass(frozen=True)
class ReadyDrainReadiness(DrainReadiness):
    def source_diagnostic(self):
        return NoDrainDiagnostic()

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

    def unavailable_since(self) -> tuple[float, ...]:
        return (self.diagnostic.recorded_at,)


@dataclass(frozen=True, slots=True)
class ActivityData:
    """Shared declared activity fields; observations and events own their boundary."""

    detail_limit: ClassVar[int] = 200

    thread: str
    state: ActivityState
    detail: str = ""
    timestamp: Annotated[
        float, WireName("ts"), WireRequired()
    ] = field(default_factory=time.time)

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

    diagnostic: Annotated[DrainDiagnostic | NoDrainDiagnostic, WireOmitDefault()] = (
        NoDrainDiagnostic()
    )

    @classmethod
    def create(cls, thread: str, state: ActivityState, detail: str = "") -> Self:
        """Create a new bounded status, rather than decoding an existing event."""
        return cls(thread, state, cls.format_detail(detail))

    @classmethod
    def absent(cls, thread: str) -> Activity:
        """No event: idle, with no historical clock."""
        return cls(thread, ActivityState.IDLE, timestamp=0.0)

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
                   readiness=event.diagnostic.readiness_for(owner))

    def source_event(self) -> Activity:
        """Write through the original event boundary; never persist a second state."""
        return Activity(self.thread, self.state, self.detail, self.timestamp,
                        self.readiness.source_diagnostic())

    def presentation(self, title: str) -> ThreadPresentation:
        """An acquired activity can also be presented independently of a thread view."""
        return self.readiness.presentation(self.ordinary_presentation(title))


class ActivityStoreRows(StoreRows):
    """The tables of ActivityStore; each row declares itself one."""


@dataclass(frozen=True)
class ActivityRow(ActivityStoreRows, TypedTable, Activity):
    """Current activity, with storage derived from the Activity declaration."""

    thread: Annotated[str, Column(primary_key=True)]

    @classmethod
    def of(cls, activity: Activity) -> Self:
        return cls(**{item.name: getattr(activity, item.name) for item in fields(Activity)})


class ActivityStore(ThreadOwnedState, DerivedStore):
    """One current row per thread; no event history or parallel projection."""

    tables = ActivityStoreRows

    def __init__(self, store_path: Path, stale_after: float = 120.0):
        self.root = store_path.parent
        self._stale_after = stale_after

    @staticmethod
    def _emit_in(db: sqlite3.Connection, activity: Activity) -> None:
        if activity.state.retires_thread:
            ActivityRow.delete(db, thread=activity.thread)
        else:
            ActivityRow.of(activity).upsert(db)

    def emit(self, activity: Activity) -> None:
        with self.writing() as db:
            self._emit_in(db, activity)

    def update(self, thread: str, transition: Callable[[Activity], Activity | None]) -> bool:
        """Apply the transition once, against the row in this write transaction."""
        with self.writing() as db:
            latest = {row.thread: row for row in ActivityRow.select(db, thread=thread)}
            event = transition(self._observed(thread, latest, active=True, now=time.time()))
            if event is None:
                return False
            self._emit_in(db, event)
            return True

    def current(self, thread: str, *, active: bool = False) -> Activity:
        """Latest activity for one thread; idle when stale or unknown."""
        return self.current_for((thread,), active=frozenset((thread,)) if active else frozenset())[thread]

    def _observed(self, thread: str, events: Mapping[str, Activity], *,
                  active: bool, now: float) -> Activity:
        if thread not in events:
            # Reading an absent event must never mint an actual-event timestamp.
            return Activity.absent(thread)
        latest = events[thread]
        if (
            not latest.diagnostic.holds_event_current
            and not active
            and now - latest.timestamp > self._stale_after
        ):
            return Activity(thread=thread, state=ActivityState.IDLE, timestamp=latest.timestamp)
        return latest

    def current_for(self, threads: Iterable[str], *,
                    active: frozenset[str] = frozenset()) -> dict[str, Activity]:
        """Read only the requested threads, including absent ones."""
        threads = tuple(dict.fromkeys(threads))
        with self.snapshot() as db:
            latest = {row.thread: row for thread in threads
                      for row in ActivityRow.stream(db, thread=thread)}
        return self._current_for(threads, latest, active)

    def _current_for(self, threads: Iterable[str], events: Mapping[str, Activity],
                     active: frozenset[str]) -> dict[str, Activity]:
        now = time.time()
        return {
            thread: self._observed(thread, events, active=thread in active, now=now)
            for thread in dict.fromkeys(threads)
        }

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        with self.writing() as db:
            for thread in threads:
                ActivityRow.delete(db, thread=thread.name)

    def rename_thread(self, old_name: str, new_name: str) -> None:
        with self.writing() as db:
            ActivityRow.update(db, where="thread=?", parameters=(old_name,), thread=new_name)
