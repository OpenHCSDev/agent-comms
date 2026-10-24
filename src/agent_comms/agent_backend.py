"""Agent runtimes behind one interface: the AgentBackend family and its records.

A thread names its backend (``Thread.backend``). Core asks that backend for
everything that touches the agent runtime -- running turns, steering, context
size, compaction, history, forks, model choice and crash recovery -- and only
ever sees the records declared here. A backend converts its runtime's own
types to these records; no consumer outside a backend reads runtime types.

Capabilities are mixin roles composed through the MRO. Thread-serving backends
inherit ``ThreadServingBackend``; new-thread selection uses ``NewThreadBackend``.
Their inherited abstract methods enforce the required behavior. Sealing binds
each concrete backend to its location and freezes the registry.

This module is deliberately light: it is imported by ``threads`` and must not
import any runtime implementation at module level.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Self

from .declared_family import DeclaredFamily
from .field_codec import InputId
from .pi_counts import PiCount
from .pi_vocabulary import CompactionReason, ThinkingLevelField
from .send_outcome import SendBoundary
from .tool_result import DeclaredReply
from .turn_outcome import TurnOutcome

if TYPE_CHECKING:
    from .agent_events import AgentEvent
    from .image_input import ImageInput
    from .launch_runtime import LaunchRuntime
    from .native_tool_call import NativeToolMode
    from .pi_events import DialogUiRequest, ExtensionUiChoice
    from .thread_model import ThreadModel
    from .threads import Thread
    from .transcript_sources import TranscriptSource
    from .turn_context import InputContributionCoordinates
    from .turn_runner import TurnRunner


class BackendDeclarationError(TypeError):
    """A backend declaration is incomplete; the message names every missing piece."""


class RuntimeLocation(DeclaredFamily, affix="Runtime"):
    """Runtime behavior derived once by the owning thread record."""

    path: str
    _backend: ClassVar[type[AgentBackend]]
    resumable: ClassVar[bool] = True

    @property
    def backend(self):
        return self._backend

    def require_forkable(self):
        pass

    async def compact(self, operation):
        return await operation()

    def new_location(self, root, worktree):
        return self

    def arguments(self):
        return {"session_file": self.read_key}

    def read_keys(self):
        return (self.read_key,)

    def transcript_cursor(self, frontier, offset, *, source):
        return frontier.cursor(source.read_key, offset)

    def history_locations(self, thread, registry, visited):
        yield self, False
        yield from thread.inherited_history(registry, visited)

    def external_records(self, root, incarnation):
        raise ValueError("A native session cannot publish an external transcript")

    def select_session(self, directory, execution, source_reads, *, participant):
        raise BackendDeclarationError('Runtime cannot select native sessions')

    @abstractmethod
    def history_source(self, root, incarnation) -> TranscriptSource: ...

    def imported_sources(self):
        return ()

    @property
    def read_key(self) -> str:
        return self.path

    @abstractmethod
    def storage_files(self): ...

    @property
    def model_choice(self):
        return None


# ---------------------------------------------------------------------------
# Records Core owns. Every backend converts its runtime's values to these.
# ---------------------------------------------------------------------------


class BusyInputPolicy(DeclaredFamily, affix="InputPolicy"):
    """What the backend does with an input that arrives while a run is going."""

    @classmethod
    @abstractmethod
    def streaming(cls) -> type[StreamingInputPolicy]: ...


class StreamingInputPolicy(BusyInputPolicy, declared_name=None):
    """The busy policies Pi's streamingBehavior protocol accepts."""

    @classmethod
    def streaming(cls) -> type[StreamingInputPolicy]:
        return cls


class FollowUpInputPolicy(StreamingInputPolicy, declared_name="followUp"):
    """Run after the current run ends; preserve Pi's exact followUp spelling."""


class SteerInputPolicy(StreamingInputPolicy):
    """Place the input at the next boundary of the current run."""


class RejectInputPolicy(BusyInputPolicy):
    @classmethod
    def streaming(cls) -> type[StreamingInputPolicy]:
        raise ValueError("Pi cannot reject a prompt through streamingBehavior.")


@dataclass(frozen=True, kw_only=True)
class InputContent:
    """What one input says: its text, attached images and context contributions."""

    text: str
    images: tuple[ImageInput, ...] = ()
    contributions: tuple[InputContributionCoordinates, ...] = ()


class TurnInboxItem(ABC):
    """An inbox item carries its input identities; the forwarding owner dispatches it."""

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return ()

    def refuse(self, inputs, turn):
        inputs.refuse(turn, self.carried_inputs)


class TurnInbox(asyncio.Queue[TurnInboxItem]):
    """The live inbox parks its head until the existing observer sees READY."""
    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()
        self.ready.set()

    async def get(self) -> TurnInboxItem:
        await self.ready.wait()
        return await super().get()

    @property
    def has_ready_work(self) -> bool:
        """Only an open inbox can give queued work to the running turn."""
        return self.ready.is_set() and not self.empty()

    @property
    def all_written_started(self):
        return not any(item.carried_inputs for item in self._queue)

    def begin(self, item: TurnInboxItem) -> None:
        self._queue.appendleft(item)
        self._wakeup_next(self._getters)

    def hold(self, item: TurnInboxItem) -> None:
        # Park under the shared wire lock, before READY can be published.
        self.ready.clear()
        self.begin(item)


@dataclass(frozen=True, kw_only=True)
class InputRequest(TurnInboxItem):
    """One queued user input handed to a backend: identity, content and busy policy.

    Routing, goal and bus-delivery provenance stay in Core, keyed by ``input_id``.
    """

    input_id: InputId
    content: InputContent
    when_busy: type[BusyInputPolicy]

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return (self.input_id,)



@dataclass(frozen=True)
class SendNow(TurnInboxItem):
    """Ask the running turn to place these queued steering inputs immediately.

    Only a backend with the :class:`Interrupts` role accepts it.
    """

    input_ids: tuple[InputId, ...]



@dataclass(frozen=True)
class UseSettings(TurnInboxItem):
    """Ask the running turn to use the thread's stored model and thinking level.

    The backend applies them from its next model call; the call in flight
    finishes on the settings it started with.
    """

    model: ThreadModel
    thinking_level: ThinkingLevelField



@dataclass(frozen=True)
class ClearQueue(TurnInboxItem):
    """Clear only these queued user inputs, never delivered bus inputs."""

    input_ids: tuple[InputId, ...]



class CompactionOutcome(DeclaredFamily, DeclaredReply, TurnOutcome, affix="Compaction"):
    """The result of one requested compaction."""

    def completion_event[T](self, event: Callable[..., T], reason: type[CompactionReason]) -> T:
        return event(reason=reason, aborted=True)


@dataclass(frozen=True)
class PlacedCompaction(CompactionOutcome):
    """A summary now replaces the entries before ``first_kept``."""

    summary: str
    first_kept: str | None
    tokens_before: PiCount

    def completion_event[T](self, event: Callable[..., T], reason: type[CompactionReason]) -> T:
        return event(reason=reason, aborted=False, summary=self.summary)


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


# ---------------------------------------------------------------------------
# Capability roles.
# ---------------------------------------------------------------------------


class InputStartListener(ABC):
    """The listener owns acknowledgement and the corresponding start notification."""

    started_events: tuple[AgentEvent, ...]

    @abstractmethod
    def original(self, native_id: InputId, text: str) -> bool: ...

    @abstractmethod
    def following(self, public_id: InputId, native_id: InputId, text: str) -> bool: ...


class NoInputStartListener(InputStartListener):
    started_events = ()

    def original(self, native_id: InputId, text: str) -> bool:
        return True

    def following(self, public_id: InputId, native_id: InputId, text: str) -> bool:
        return True


NO_INPUT_START_LISTENER = NoInputStartListener()


@dataclass(frozen=True, kw_only=True)
class TurnDelivery:
    """Core's bindings for one turn, handed to the backend that runs it.

    ``inbox`` carries :class:`InputRequest` and :class:`SendNow` records that
    arrive while the turn runs. Core checks input permission at these boundaries: the
    backend asks ``send_boundary`` before writing each input or interrupt;
    ``input_start`` reports that the
    runtime began an input; ``request_progress`` and ``ui_request`` observe
    provider progress and answer runtime dialogs.
    """

    worktree: str
    environment: dict[str, str]
    inbox: TurnInbox
    finish_event: asyncio.Event
    send_boundary: SendBoundary
    input_start: InputStartListener
    request_progress: Callable[..., None]
    ui_request: Callable[[DialogUiRequest], Awaitable[ExtensionUiChoice]]
    tool_mode: NativeToolMode


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
    async def abort[Result](self, owner: asyncio.Task[Result]) -> None:
        """Stop the runtime work owned by this turn task."""

    @abstractmethod
    async def close_idle(self) -> None:
        """Retire the runtime process this backend holds once no turn is using it."""


class Steers(RunsTurns):
    """Accepts :class:`InputRequest` with ``SteerInputPolicy`` while a run is going."""


class Interrupts(Steers):
    """Accepts :class:`SendNow`: ends the current tool round and places queued steers now."""


class Compacts(ABC):
    """The backend owns when and how to compact. Core only asks for a manual one."""

    @abstractmethod
    async def compact(self, thread: Thread, instructions: str | None) -> CompactionOutcome:
        """Compact ``thread`` now, guided by ``instructions``."""


class Forks(ABC):
    """Creates a child runtime location from a parent thread's history."""

    @classmethod
    @abstractmethod
    async def fork(cls, parent: Thread, launcher: str, runtime: LaunchRuntime) -> RuntimeLocation:
        """Copy ``parent``'s history into a new location for a child thread.

        ``runtime`` is the same selected installation that will start the child.
        """


# ---------------------------------------------------------------------------
# The family.
# ---------------------------------------------------------------------------


class AgentBackend(DeclaredFamily, affix="Backend"):
    """One agent runtime, keyed by ``declared_name`` ("pi_native", ...).

    A backend declares its location type, capabilities through inheritance,
    and the event kinds its runtime protocol emits. One
    instance holds one thread session's runtime process.
    """

    location_type: ClassVar[type[RuntimeLocation]]
    event_kinds: ClassVar[frozenset[str]]
    _sealed: ClassVar[bool] = False

    async def context_for_input(self, thread, context):
        return context

    def __init_subclass__(cls, **options: str | None) -> None:
        if AgentBackend._sealed:
            raise BackendDeclarationError(
                f"{cls.__name__}: the AgentBackend registry is sealed; declare backends "
                "before the Thread backend lookup seals its declarations."
            )
        super().__init_subclass__(**options)

    @classmethod
    @abstractmethod
    def for_worker(cls, runner: TurnRunner, session_id: str) -> Self:
        """An instance holding this runtime for one thread session in this worker."""

    @classmethod
    @abstractmethod
    def new_location(cls, root, worktree) -> RuntimeLocation: ...

    @classmethod
    def seal(cls) -> None:
        """Bind concrete locations after validating all declaration relationships."""
        NewThreadBackend.selected()
        owners: dict[type[RuntimeLocation], type[AgentBackend]] = {}
        for member in cls.members_with(cls):
            location = member.location_type
            if not issubclass(location, RuntimeLocation):
                raise BackendDeclarationError(f"{member.__name__} declares no RuntimeLocation type")
            if previous := owners.get(location):
                raise BackendDeclarationError(
                    f"location type {location.__name__} belongs to several backends "
                    f"{previous.__name__}, {member.__name__}")
            owners[location] = member
        for location, owner in owners.items():
            location._backend = owner
        AgentBackend.__registry__ = MappingProxyType(dict(AgentBackend.__registry__))  # type: ignore[assignment]
        AgentBackend._sealed = True

    def run_turn(self, thread: Thread, content: InputContent, delivery: TurnDelivery):
        raise BackendDeclarationError(f"{type(self).__name__} does not run turns")

    @classmethod
    def require_event_family(cls, family: type[DeclaredFamily]) -> None:
        """Fail unless ``family`` converts exactly the event kinds this backend's runtime emits."""
        problems = _event_family_problems(cls, family)
        if problems:
            raise BackendDeclarationError(f"{cls.__name__}: " + "; ".join(problems))


def _event_family_problems(backend: type, family: type[DeclaredFamily]) -> list[str]:
    declared = {kind for member in family.members_with(family) for kind in member.known_kinds()}
    expected = backend.event_kinds
    problems = []
    if missing := sorted(expected - declared):
        problems.append(f"{family.__name__} has no converter for event kinds {missing}")
    if extra := sorted(declared - expected):
        problems.append(f"{family.__name__} converts event kinds the runtime never emits {extra}")
    return problems


class ThreadServingBackend(Steers, Compacts, AgentBackend, declared_name=None):
    """A thread backend implements steering, turn lifecycle and compaction together.

    The capability bases precede AgentBackend so its non-thread turn refusal
    cannot satisfy the abstract turn contract.
    """


class NewThreadBackend(ThreadServingBackend, declared_name=None):
    """The complete thread backend selected for newly registered threads."""

    @classmethod
    def selected(cls) -> type[Self]:
        members = cls.members_with(cls)
        if len(members) != 1:
            raise BackendDeclarationError(
                f"exactly one backend must serve new threads, found "
                f"{[member.__name__ for member in members]}")
        return members[0]

