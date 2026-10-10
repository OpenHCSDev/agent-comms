"""Agent runtimes behind one interface: the AgentBackend family and its records.

A thread names its backend (``Thread.backend``). Core asks that backend for
everything that touches the agent runtime -- running turns, steering, context
size, compaction, history, forks, model choice and crash recovery -- and only
ever sees the records declared here. A backend converts its runtime's own
types to these records; no consumer outside a backend reads runtime types.

Capabilities are mixin roles composed through the MRO. A backend that serves
threads must implement every role in ``THREAD_CAPABILITIES``; the abstract
methods enforce the signatures, and :meth:`AgentBackend.seal` checks the rest
once every backend module is declared, then freezes the registry.

This module is deliberately light: it is imported by ``threads`` and must not
import any runtime implementation at module level.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Self

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .agent_events import AgentEvent
    from .image_inputs import ImageInput
    from .threads import Thread
    from .turn_context import InputContributionCoordinates


class BackendDeclarationError(TypeError):
    """A backend declaration is incomplete; the message names every missing piece."""


class RuntimeLocation(ABC):
    """Where one thread's runtime state lives, as its backend understands it.

    Each backend declares exactly one location type. A location is derived from
    the thread record that owns the fact; it never stores a second copy of it.
    """

    @classmethod
    @abstractmethod
    def of(cls, thread: Thread) -> Self:
        """The location of this thread's runtime state."""


# ---------------------------------------------------------------------------
# Records Core owns. Every backend converts its runtime's values to these.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputId:
    """Core's identity for one input; a durable backend uses it as its request ID."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value:
            raise ValueError("An input ID must be a nonempty string")


class WhenBusy(Enum):
    """What the backend does with an input that arrives while a run is going."""

    FOLLOW_UP = "follow_up"  # run it after the current run ends
    STEER = "steer"  # place it at the next boundary of the current run
    REJECT = "reject"  # refuse it


@dataclass(frozen=True, kw_only=True)
class InputContent:
    """What one input says: its text, attached images and context contributions."""

    text: str
    images: tuple[ImageInput, ...] = ()
    contributions: tuple[InputContributionCoordinates, ...] = ()


@dataclass(frozen=True, kw_only=True)
class InputRequest:
    """One queued user input handed to a backend: identity, content and busy policy.

    Routing, goal and bus-delivery provenance stay in Core, keyed by ``input_id``.
    """

    input_id: InputId
    content: InputContent
    when_busy: WhenBusy

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return (self.input_id,)

    def deliver_to(self, receiver: Any, turn: Any) -> Awaitable[bool]:
        """Hand this input to the running turn's receiver; False ends forwarding."""
        return receiver.place(turn, self)


@dataclass(frozen=True)
class SendNow:
    """Ask the running turn to place these queued steering inputs immediately.

    Only a backend with the :class:`Interrupts` role accepts it.
    """

    input_ids: tuple[InputId, ...]

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        """A control names inputs already queued; it carries no input of its own."""
        return ()

    def deliver_to(self, receiver: Any, turn: Any) -> Awaitable[bool]:
        return receiver.send_now(turn, self)


class UnansweredReason(DeclaredFamily, affix="Reason"):
    """Why an input ended without an answer."""

    @property
    def message(self) -> str:
        return self.declared_name.replace("_", " ")


@dataclass(frozen=True)
class AbortedReason(UnansweredReason):
    """The run was aborted before it answered this input."""


@dataclass(frozen=True)
class StaleReason(UnansweredReason):
    """The input was superseded before the backend placed it."""


@dataclass(frozen=True)
class NoModelReason(UnansweredReason):
    """No model was configured to answer."""


@dataclass(frozen=True)
class FailedReason(UnansweredReason):
    """The runtime failed while answering; ``text`` is its message, unchanged."""

    text: str

    @property
    def message(self) -> str:
        return self.text


@dataclass(frozen=True)
class EntryRef:
    """A position in a backend's transcript: an order key plus the backend's own ID."""

    order: int
    backend_id: str


class InputOutcome(DeclaredFamily, affix="Outcome"):
    """Where one input stands in its backend."""


@dataclass(frozen=True)
class QueuedOutcome(InputOutcome):
    """Accepted and waiting for a run boundary."""


@dataclass(frozen=True)
class PlacedOutcome(InputOutcome):
    """Placed into the transcript at ``entry``; the answer is still running."""

    entry: EntryRef


@dataclass(frozen=True)
class AnsweredOutcome(InputOutcome):
    """Answered; ``entry`` is the final assistant entry."""

    entry: EntryRef


@dataclass(frozen=True)
class UnansweredOutcome(InputOutcome):
    """Ended without an answer, for ``reason``."""

    reason: UnansweredReason


class TranscriptKind(DeclaredFamily, affix="Entry"):
    """The kind of one transcript entry."""


class UserEntry(TranscriptKind):
    """A user input."""


class AssistantEntry(TranscriptKind):
    """A model reply."""


class ToolResultEntry(TranscriptKind):
    """The result of one tool call."""


class CompactionEntry(TranscriptKind):
    """A compaction summary that replaces the entries before its first kept entry."""


class ResetEntry(TranscriptKind):
    """A boundary after which earlier entries no longer reach the model."""


class SystemEntry(TranscriptKind):
    """Runtime bookkeeping: session header, model or setting changes, labels."""


@dataclass(frozen=True)
class TranscriptEntry:
    """One transcript entry in the backend-neutral form readers page through."""

    ref: EntryRef
    kind: type[TranscriptKind]
    text: str
    at: datetime | None


@dataclass(frozen=True)
class EntryPage:
    """Entries newest first, and the cursor for the next older page (None at the start)."""

    entries: tuple[TranscriptEntry, ...]
    older: EntryRef | None


@dataclass(frozen=True)
class ContextUsage:
    """Context size measured by the backend's own estimator."""

    tokens: int | None
    window: int | None


class CompactionOutcome(DeclaredFamily, affix="Compaction"):
    """The result of one requested compaction."""


@dataclass(frozen=True)
class PlacedCompaction(CompactionOutcome):
    """A summary now replaces the entries before ``first_kept``."""

    summary: str
    first_kept: str | None
    tokens_before: int | None


@dataclass(frozen=True)
class NothingToCompaction(CompactionOutcome):
    """There was nothing to summarize."""


@dataclass(frozen=True)
class FailedCompaction(CompactionOutcome):
    """The runtime refused or failed; ``message`` is its text, unchanged."""

    message: str


@dataclass(frozen=True)
class StaleCompaction(CompactionOutcome):
    """The transcript moved past the summary's cut before it was placed."""


class InterruptedWork(DeclaredFamily, affix="Work"):
    """Work a crashed runtime left unfinished, listed before anything resumes."""


@dataclass(frozen=True)
class InterruptedRequestWork(InterruptedWork):
    """A model request that was running for these inputs."""

    inputs: tuple[InputId, ...]


@dataclass(frozen=True)
class InterruptedToolWork(InterruptedWork):
    """A tool call that was running; ``replay_safe`` says whether it may run again."""

    call_id: str
    replay_safe: bool


class Disposition(Enum):
    """What Core decided to do with one piece of interrupted work."""

    RESUME = "resume"
    ABANDON = "abandon"


# ---------------------------------------------------------------------------
# Capability roles.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TurnDelivery:
    """Core's bindings for one turn, handed to the backend that runs it.

    ``inbox`` carries :class:`InputRequest` and :class:`SendNow` records that
    arrive while the turn runs. The boundaries are Core's admission checks: the
    backend enters ``send_boundary`` around writing each input and
    ``interrupt_boundary`` around a send-now; ``input_started`` reports that the
    runtime began an input; ``request_progress`` and ``ui_request`` observe
    provider progress and answer runtime dialogs.
    """

    worktree: str
    environment: dict[str, str]
    inbox: asyncio.Queue[InputRequest | SendNow]
    finish_event: asyncio.Event
    send_boundary: Callable[..., Any]
    interrupt_boundary: Callable[..., Any]
    input_started: Callable[..., bool]
    request_progress: Callable[..., None]
    ui_request: Callable[..., Awaitable[Any]]


class RunsTurns(ABC):
    """Runs one input to settlement and reports what happened as AgentEvents.

    The final event of a turn is ``agent_events.Done``.
    """

    @abstractmethod
    def run_turn(
        self, thread: Thread, content: InputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        """Run ``content`` as ``thread``'s next input and yield its events."""

    @abstractmethod
    async def abort(self, owner: asyncio.Task[Any]) -> None:
        """Stop the runtime work owned by this turn task."""

    @abstractmethod
    async def close_idle(self) -> None:
        """Retire the runtime process this backend holds once no turn is using it."""


class Steers(RunsTurns):
    """Accepts :class:`InputRequest` with ``WhenBusy.STEER`` while a run is going."""


class Interrupts(Steers):
    """Accepts :class:`SendNow`: ends the current tool round and places queued steers now."""


class Compacts(ABC):
    """The backend owns when and how to compact. Core only asks for a manual one."""

    @abstractmethod
    async def compact(self, thread: Thread, instructions: str | None) -> CompactionOutcome:
        """Compact ``thread`` now, guided by ``instructions``."""


class ReportsContext(ABC):
    """Context size from the backend's own estimator. Core never estimates."""

    @abstractmethod
    async def context_usage(self, thread: Thread) -> ContextUsage:
        """The thread's current context size."""


class ReadsHistory(ABC):
    """Pages of the transcript, newest first, for lazy history."""

    @abstractmethod
    async def entries(self, thread: Thread, before: EntryRef | None, limit: int) -> EntryPage:
        """Up to ``limit`` entries older than ``before`` (or the newest when None)."""


class Forks(ABC):
    """Creates a child runtime location from a parent thread's history."""

    @classmethod
    @abstractmethod
    async def fork(cls, parent: Thread, root: Path, launcher: str) -> RuntimeLocation:
        """Copy ``parent``'s history into a new location for a child thread.

        ``launcher`` is the runtime command the caller would start the child with.
        """


class RecoversAfterCrash(ABC):
    """Lists interrupted work before anything resumes. Nothing runs until it is settled."""

    @abstractmethod
    async def interrupted(self, thread: Thread) -> tuple[InterruptedWork, ...]:
        """Work the runtime left unfinished for this thread."""

    @abstractmethod
    async def settle(self, thread: Thread, work: InterruptedWork, disposition: Disposition) -> None:
        """Apply Core's decision to one piece of interrupted work."""

    @abstractmethod
    async def resume(self, thread: Thread) -> None:
        """Start scheduling again once every interrupted piece is settled."""


class ConfiguresModel(ABC):
    """Lists the models the runtime can use and applies a model / thinking choice."""

    @abstractmethod
    async def models(self, thread: Thread) -> tuple[str, ...]:
        """Model names, as ``provider/model``, available to this thread."""

    @abstractmethod
    async def configure(self, thread: Thread, model: str | None, thinking: str | None) -> tuple[str, str]:
        """Apply the choice and return the ``(model, thinking level)`` now in effect."""


class ImportsHistory(ABC):
    """Writes another backend's transcript into a new location of this backend."""

    @classmethod
    @abstractmethod
    async def import_from(cls, source: ReadsHistory, thread: Thread) -> RuntimeLocation:
        """Import ``source``'s history for ``thread`` into a new location."""


THREAD_CAPABILITIES: tuple[type, ...] = (
    Steers,
    Compacts,
    ReportsContext,
    ReadsHistory,
    RecoversAfterCrash,
    ConfiguresModel,
)


# ---------------------------------------------------------------------------
# The family.
# ---------------------------------------------------------------------------


class AgentBackend(DeclaredFamily, affix="Backend"):
    """One agent runtime, keyed by ``declared_name`` ("pi_native", ...).

    A backend declares its location type, whether it serves threads, whether
    new threads get it, and the event kinds its runtime protocol emits. One
    instance holds one thread session's runtime process.
    """

    location_type: ClassVar[type[RuntimeLocation]]
    serves_threads: ClassVar[bool] = True
    serves_new_threads: ClassVar[bool] = False
    event_kinds: ClassVar[frozenset[str]]
    _sealed: ClassVar[bool] = False

    def __init_subclass__(cls, **options: Any) -> None:
        if AgentBackend._sealed:
            raise BackendDeclarationError(
                f"{cls.__name__}: the AgentBackend registry is sealed; declare backends "
                "in a module imported by agent_backend before it seals."
            )
        super().__init_subclass__(**options)

    @classmethod
    @abstractmethod
    def for_worker(cls, runner: Any, session_id: str) -> Self:
        """An instance holding this runtime for one thread session in this worker."""

    @classmethod
    def for_new_threads(cls) -> type[AgentBackend]:
        """The backend a newly declared thread gets."""
        (member,) = (m for m in AgentBackend.members_with(AgentBackend) if m.serves_new_threads)
        return member

    @classmethod
    def seal(cls) -> None:
        """Validate every declared backend and freeze the registry."""
        validate_backends(dict(AgentBackend.__registry__).values())
        AgentBackend.__registry__ = MappingProxyType(dict(AgentBackend.__registry__))  # type: ignore[assignment]
        AgentBackend._sealed = True

    @classmethod
    def require_event_family(cls, family: type[DeclaredFamily]) -> None:
        """Fail unless ``family`` converts exactly the event kinds this backend's runtime emits."""
        problems = _event_family_problems(cls, family)
        if problems:
            raise BackendDeclarationError(f"{cls.__name__}: " + "; ".join(problems))


def _event_family_problems(backend: type, family: type[DeclaredFamily]) -> list[str]:
    declared = {member.wire_kind() for member in family.members_with(family) if not member.unknown_kind}
    expected = backend.event_kinds
    problems = []
    if missing := sorted(expected - declared):
        problems.append(f"{family.__name__} has no converter for event kinds {missing}")
    if extra := sorted(declared - expected):
        problems.append(f"{family.__name__} converts event kinds the runtime never emits {extra}")
    return problems


def validate_backends(members: Iterable[type]) -> None:
    """Check backend declarations; raise once, naming every missing piece."""
    members = tuple(members)
    problems: list[str] = []
    owners: dict[type, list[str]] = {}
    for member in members:
        name = member.__name__
        location = getattr(member, "location_type", None)
        if not (isinstance(location, type) and issubclass(location, RuntimeLocation)):
            problems.append(f"{name} declares no RuntimeLocation type")
        else:
            owners.setdefault(location, []).append(name)
        if not isinstance(getattr(member, "event_kinds", None), frozenset):
            problems.append(f"{name} declares no event_kinds")
        if member.serves_threads:
            missing = [role.__name__ for role in THREAD_CAPABILITIES if not issubclass(member, role)]
            if missing:
                problems.append(f"{name} serves threads but lacks roles {missing}")
        unimplemented = sorted(getattr(member, "__abstractmethods__", ()))
        if unimplemented:
            problems.append(f"{name} leaves methods abstract {unimplemented}")
    for location, names in owners.items():
        if len(names) > 1:
            problems.append(f"location type {location.__name__} belongs to several backends {names}")
    new_thread_backends = [member.__name__ for member in members if member.serves_new_threads]
    if len(new_thread_backends) != 1:
        problems.append(f"exactly one backend must serve new threads, found {new_thread_backends}")
    if problems:
        raise BackendDeclarationError("; ".join(problems))


# Every backend module is declared before the registry seals.
from . import pi_native_backend  # noqa: E402,F401

AgentBackend.seal()
