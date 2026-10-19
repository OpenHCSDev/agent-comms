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
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, Self

from .declared_family import DeclaredFamily
from .pi_counts import PiCount
from .pi_vocabulary import ThinkingLevelField
from .send_outcome import SendBoundary
from .tool_result import DeclaredReply

if TYPE_CHECKING:
    from .agent_events import AgentEvent
    from .image_inputs import ImageInput
    from .launch_runtime import LaunchRuntime
    from .native_tool_call import NativeToolMode
    from .threads import Thread
    from .turn_context import InputContributionCoordinates


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

    def select_session(self, directory, execution, source_reads):
        raise BackendDeclarationError('Runtime cannot select native sessions')

    @abstractmethod
    def history_source(self, root, incarnation): ...

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


class TurnInboxReceiver(Protocol):
    """What a running turn does with each record its inbox carries."""

    def place(self, turn: object, item: InputRequest) -> Awaitable[bool]: ...
    def send_now(self, turn: object, item: SendNow) -> Awaitable[bool]: ...
    def use_settings(self, turn: object, item: UseSettings) -> Awaitable[bool]: ...
    def clear_queue(self, turn: object, item: ClearQueue) -> Awaitable[bool]: ...


class TurnInbox(asyncio.Queue):
    """The live inbox parks its head until the existing observer sees READY."""
    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()
        self.ready.set()

    async def get(self):
        await self.ready.wait()
        return await super().get()

    @property
    def has_ready_work(self) -> bool:
        """Only an open inbox can give queued work to the running turn."""
        return self.ready.is_set() and not self.empty()

    @property
    def all_written_started(self):
        return not any(item.carried_inputs for item in self._queue)

    def begin(self, item):
        self._queue.appendleft(item)
        self._wakeup_next(self._getters)

    def hold(self, item):
        # Park under the shared wire lock, before READY can be published.
        self.ready.clear()
        self.begin(item)


class TurnInboxItem(ABC):
    """An inbox item owns its carried inputs and delivery to a running turn."""

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return ()

    def refuse(self, inputs, turn):
        inputs.refuse(turn, self.carried_inputs)

    @abstractmethod
    def deliver_to(self, receiver: TurnInboxReceiver, turn: object) -> Awaitable[bool]: ...


@dataclass(frozen=True, kw_only=True)
class InputRequest(TurnInboxItem):
    """One queued user input handed to a backend: identity, content and busy policy.

    Routing, goal and bus-delivery provenance stay in Core, keyed by ``input_id``.
    """

    input_id: InputId
    content: InputContent
    when_busy: WhenBusy

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return (self.input_id,)

    def deliver_to(self, receiver: TurnInboxReceiver, turn: object) -> Awaitable[bool]:
        """Hand this input to the running turn's receiver; False ends forwarding."""
        return receiver.place(turn, self)


@dataclass(frozen=True)
class SendNow(TurnInboxItem):
    """Ask the running turn to place these queued steering inputs immediately.

    Only a backend with the :class:`Interrupts` role accepts it.
    """

    input_ids: tuple[InputId, ...]

    def deliver_to(self, receiver: TurnInboxReceiver, turn: object) -> Awaitable[bool]:
        return receiver.send_now(turn, self)


@dataclass(frozen=True)
class UseSettings(TurnInboxItem):
    """Ask the running turn to use the thread's stored model and thinking level.

    The backend applies them from its next model call; the call in flight
    finishes on the settings it started with.
    """

    model: str
    thinking_level: ThinkingLevelField

    def deliver_to(self, receiver: TurnInboxReceiver, turn: object) -> Awaitable[bool]:
        return receiver.use_settings(turn, self)


@dataclass(frozen=True)
class ClearQueue(TurnInboxItem):
    """Clear only these queued user inputs, never delivered bus inputs."""

    input_ids: tuple[InputId, ...]

    def deliver_to(self, receiver: TurnInboxReceiver, turn: object) -> Awaitable[bool]:
        return receiver.clear_queue(turn, self)


class CompactionOutcome(DeclaredFamily, DeclaredReply, affix="Compaction"):
    """The result of one requested compaction."""


@dataclass(frozen=True)
class PlacedCompaction(CompactionOutcome):
    """A summary now replaces the entries before ``first_kept``."""

    summary: str
    first_kept: str | None
    tokens_before: PiCount


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


@dataclass(frozen=True, kw_only=True)
class TurnDelivery:
    """Core's bindings for one turn, handed to the backend that runs it.

    ``inbox`` carries :class:`InputRequest` and :class:`SendNow` records that
    arrive while the turn runs. Core checks input permission at these boundaries: the
    backend enters ``send_boundary`` around writing each input and
    ``interrupt_boundary`` around a send-now; ``input_started`` reports that the
    runtime began an input; ``request_progress`` and ``ui_request`` observe
    provider progress and answer runtime dialogs.
    """

    worktree: str
    environment: dict[str, str]
    inbox: TurnInbox
    finish_event: asyncio.Event
    send_boundary: SendBoundary
    interrupt_boundary: SendBoundary
    input_started: Callable[..., bool]
    request_progress: Callable[..., None]
    ui_request: Callable[..., Awaitable[Any]]
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


class Forks(ABC):
    """Creates a child runtime location from a parent thread's history."""

    @classmethod
    @abstractmethod
    async def fork(cls, parent: Thread, launcher: str, runtime: LaunchRuntime) -> RuntimeLocation:
        """Copy ``parent``'s history into a new location for a child thread.

        ``runtime`` is the same selected installation that will start the child.
        """


class ConfiguresModel(ABC):
    """Lists the models the runtime can use and applies a model / thinking choice."""

    @abstractmethod
    async def models(self, thread: Thread) -> tuple[str, ...]:
        """Model names, as ``provider/model``, available to this thread."""

    @abstractmethod
    async def configure(self, thread: Thread, model: str | None, thinking: str | None) -> tuple[str, str]:
        """Apply the choice and return the ``(model, thinking level)`` now in effect."""


THREAD_CAPABILITIES: tuple[type, ...] = (
    Steers,
    Compacts,
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
    unstarted_type: ClassVar[type[RuntimeLocation]]
    serves_threads: ClassVar[bool] = True
    serves_new_threads: ClassVar[bool] = False
    event_kinds: ClassVar[frozenset[str]]
    _sealed: ClassVar[bool] = False

    async def context_for_input(self, thread, context):
        return context

    def __init_subclass__(cls, **options: Any) -> None:
        if AgentBackend._sealed:
            raise BackendDeclarationError(
                f"{cls.__name__}: the AgentBackend registry is sealed; declare backends "
                "before the Thread backend lookup seals its declarations."
            )
        super().__init_subclass__(**options)
        cls.location_type._backend = cls

    @classmethod
    @abstractmethod
    def for_worker(cls, runner: Any, session_id: str) -> Self:
        """An instance holding this runtime for one thread session in this worker."""

    @classmethod
    @abstractmethod
    def new_location(cls, root, worktree) -> RuntimeLocation: ...

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

    def run_turn(self, thread: Thread, content: InputContent, delivery: TurnDelivery):
        raise BackendDeclarationError(f"{type(self).__name__} does not run turns")


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

