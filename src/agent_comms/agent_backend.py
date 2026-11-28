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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Self

from metaclass_registry.core import REPLACES_REGISTERED_ATTRIBUTE
from metaclass_registry.discovery import import_module_preserving_root_logging

from agent_comms.errors import RelationViolationError
from agent_comms_hooks import ToolRefusedError
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.backend_query import (
    BackendQuery,
    BackendQueryResponse,
    ContextCommitProof,
    TurnInputContent,
)
from agent_comms.execution.child_process import NoProcess, ProcessState
from agent_comms.execution.history_view import OwnHistory
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.native_arguments import ModelProvider
from agent_comms.execution.pi_vocabulary import CompactionReason
from agent_comms.execution.session_revision import NoSessionObservation, SessionObservation
from agent_comms.execution.transcript_cursor_reads import (
    TranscriptHistorySource,
    TranscriptRuntime,
)
from agent_comms.execution.turn_outcome import TurnOutcome
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily, FamilyMember
from agent_comms.nominal.field_codec import AcpSessionId, InputId, TurnId
from agent_comms.nominal.served_declaration import ServesDeclaration
from agent_comms.pi_counts import PiCount
from agent_comms.registry.thread_identity import (
    RecordedThreadIncarnation,
    ThreadIncarnation,
)
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel
from agent_comms.transport.tool_result import DeclaredReply

if TYPE_CHECKING:
    from agent_comms.execution.owned_turn import TurnDelivery
    from agent_comms.execution.turn_runner import TurnRunner
    from agent_comms.registry.threads import Thread


class TurnPublicationRecorder[Lease, Cursor, Routing](ABC):
    """Transcript store capability that joins a completed native turn to committed wire rows."""

    @abstractmethod
    def record_native_turn_publication(
        self,
        *,
        lease: Lease,
        checkpoint: Cursor,
        routing: Routing,
        published: tuple[MessageReference, ...],
        incarnation: RecordedThreadIncarnation,
        thread: Thread,
    ) -> None: ...


class ThreadSettingApplier[Configuration, Command](ABC):
    """Configuration owner capability applying stored model/thinking updates for a backend."""

    @abstractmethod
    async def configure_pi_thread(
        self, configuration: Configuration, thread: Thread, command: Command
    ) -> None: ...

    @abstractmethod
    async def store_direct_setting(self, thread: Thread, command: Command) -> None: ...


class OwnedInboxDrainer(ABC):
    """Input drain capability serving either private-N/K Pi or direct ACP threads."""

    @abstractmethod
    async def drain_private_inbox(self, session_id: AcpSessionId) -> None: ...

    @abstractmethod
    async def drain_direct_inbox(self, session_id: AcpSessionId, thread: Thread) -> None: ...


class TurnInputAcknowledger[Origin, Bridge](ABC):
    """Composition-root capability recording durable delivery acknowledgements for direct inputs."""

    @abstractmethod
    def acknowledge_direct_turn_inputs(
        self,
        thread: Thread,
        origins: tuple[Origin, ...],
        *,
        bridge: type[Bridge],
    ) -> None: ...

    @abstractmethod
    def ignore_direct_turn_inputs(
        self,
        thread: Thread,
        origins: tuple[Origin, ...],
        *,
        bridge: type[Bridge],
    ) -> None: ...


class TurnTranscriptSink:
    """Per-turn transcript publisher for backends whose history lives in ``ExternalTranscript``."""

    async def on_thinking(self, text: str) -> None:
        del text

    async def on_chunk(self, text: str) -> None:
        del text

    async def on_tool_start(self, event: AgentEvent) -> None:
        del event

    async def on_tool_end(self, event: AgentEvent) -> None:
        del event

    def on_committed_progress(self, text: str, published: tuple[MessageReference, ...]) -> None:
        del text, published

    def on_finish(self, published: tuple[MessageReference, ...]) -> None:
        del published

    def on_abort(self) -> None:
        pass


class BackendDeclarationError(TypeError):
    """A backend declaration is incomplete; the message names every missing piece."""


class RuntimeLocation(DeclaredFamily, TranscriptRuntime, affix='Runtime'):
    """Runtime behavior derived once by the owning thread record."""

    path: str
    _backend: ClassVar[type[AgentBackend]]
    def is_resumable(self) -> bool:
        return True

    @property
    def backend(self):
        return self._backend

    def require_forkable(self):
        pass

    def last_written(self) -> float:
        """When the native transcript last grew; one never written grew at the epoch."""
        transcript = Path(self.path)
        return transcript.stat().st_mtime if transcript.exists() else 0.0

    async def compact(self, operation):
        return await operation()

    def new_location(self, root, worktree):
        return self

    def fork_session_path(self, root: Path, worktree: Path) -> tuple[type[AgentBackend], str]:
        return (self.backend, self.read_key)

    def arguments(self):
        return {"session_file": self.read_key}

    def read_keys(self):
        return (self.read_key,)

    def native_cut(self, key: str):
        raise BackendDeclarationError(f"{type(self).__name__} owns no native transcript file")

    def transcript_cursor(self, frontier, cut, *, source):
        return frontier.cursor(source.read_key, cut.offset, file=cut.file_identity())

    def record_turn_publication[Lease, Cursor, Routing](
        self,
        transcripts: TurnPublicationRecorder[Lease, Cursor, Routing],
        *,
        lease: Lease,
        checkpoint: Cursor,
        routing: Routing,
        published: tuple[MessageReference, ...],
        incarnation: RecordedThreadIncarnation,
        thread: Thread,
    ) -> None:
        transcripts.record_native_turn_publication(
            lease=lease,
            checkpoint=checkpoint,
            routing=routing,
            published=published,
            incarnation=incarnation,
            thread=thread,
        )

    def history_locations(self, thread, registry, visited):
        yield self, OwnHistory()
        yield from thread.inherited_history(registry, visited)

    def view_from_fork(self, view, fork_birth):
        """A saved session holds its owner's records, so a fork sees them only up to the fork."""
        return view.seen_from_fork(fork_birth)

    def external_records(self, root, incarnation):
        raise ValueError("A native session cannot publish an external transcript")

    def select_session(self, directory, execution, source_reads, *, participant):
        raise BackendDeclarationError('Runtime cannot select native sessions')

    @abstractmethod
    def history_source(self, root, incarnation) -> TranscriptHistorySource: ...

    def imported_sources(self):
        return ()

    @property
    def read_key(self) -> str:
        return self.path

    @abstractmethod
    def storage_files(self): ...

    def session_observation(self, root: Path, thread: Thread) -> SessionObservation:
        return NoSessionObservation()

    @property
    def saved_model(self) -> ThreadModel:
        """A runtime without a saved session file selects no model."""
        return UndeclaredModel()


# ---------------------------------------------------------------------------
# Records Core owns. Every backend converts its runtime's values to these.
# ---------------------------------------------------------------------------


class CompactionOutcome(DeclaredFamily, DeclaredReply, TurnOutcome, affix="Compaction"):
    """The result of one requested compaction."""

    def completion_event[T](self, end: Callable[..., T], abort: Callable[..., T],
                            reason: type[CompactionReason]) -> T:
        return abort(reason=reason)


@dataclass(frozen=True)
class PlacedCompaction(CompactionOutcome):
    """A summary now replaces the entries Pi's compaction entry names."""

    summary: str
    tokens_before: PiCount

    def completion_event[T](self, end: Callable[..., T], abort: Callable[..., T],
                            reason: type[CompactionReason]) -> T:
        return end(reason=reason)


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

    async def original_context_committed(self, sent_text: str, context: ContextCommitProof) -> None:
        """An unowned input has no durable ordinary disposition to acknowledge."""
        return None

    async def following_context_committed(
        self,
        public_id: InputId,
        sent_text: str,
        context: ContextCommitProof,
    ) -> None:
        """An unowned following input has no durable ordinary disposition."""
        return None


class NoInputStartListener(InputStartListener):
    started_events = ()

    def original(self, native_id: InputId, text: str) -> bool:
        return True

    def following(self, public_id: InputId, native_id: InputId, text: str) -> bool:
        return True


NO_INPUT_START_LISTENER = NoInputStartListener()


class RunsTurns(ABC):
    """Runs one input to settlement and reports what happened as AgentEvents.

    The final event of a turn is ``agent_events.Done``.
    """

    @abstractmethod
    def run_turn(
        self, thread: Thread, content: TurnInputContent, delivery: TurnDelivery
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
    def is_forkable(cls) -> bool:
        return True

    @classmethod
    def require_forkable(cls, parent: Thread) -> None:
        parent.runtime.require_forkable()

    @classmethod
    @abstractmethod
    async def fork(
        cls, parent: Thread, launcher: str, runtime: LaunchRuntime, root: Path,
    ) -> RuntimeLocation:
        """Copy ``parent``'s history into a new location for a child thread under ``root``.

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
    provider_type: ClassVar[type[ModelProvider]] = ModelProvider
    _sealed: ClassVar[bool] = False
    _served_declarations: ClassVar[tuple[type[AgentBackend], ...]] = ()

    @classmethod
    def for_provider(cls, provider: type[ModelProvider]) -> type[AgentBackend]:
        """Resolve the backend declared for ``provider``, defaulting to ``NewThreadBackend``."""
        for member in AgentBackend.members_with(AgentBackend):
            if member.provider_type is provider:
                return member
        return NewThreadBackend.selected()

    @classmethod
    def configured_for(cls, provider: type[ModelProvider]) -> type[AgentBackend]:
        """Backend a thread on ``cls`` moves to when configured with a model from ``provider``."""
        return cls.for_provider(provider)

    @classmethod
    async def apply_setting[Configuration, Command](
        cls,
        owner: ThreadSettingApplier[Configuration, Command],
        configuration: Configuration,
        thread: Thread,
        command: Command,
    ) -> None:
        """Apply one runtime setting change (`SetModel` or `SetThinkingLevel`) for this backend."""
        await owner.configure_pi_thread(configuration, thread, command)

    @classmethod
    async def drain_owned_inbox(
        cls,
        inputs: OwnedInboxDrainer,
        session_id: AcpSessionId,
        thread: Thread,
    ) -> None:
        """Drain this thread's inbox inside the worker host."""
        del thread
        await inputs.drain_private_inbox(session_id)

    @classmethod
    def acknowledge_turn_inputs[Origin, Bridge](
        cls,
        acknowledger: TurnInputAcknowledger[Origin, Bridge],
        thread: Thread,
        origins: tuple[Origin, ...],
    ) -> None:
        """Record model-context delivery acknowledgements when a backend starts origins."""
        del acknowledger, thread, origins

    @classmethod
    def ignore_turn_inputs[Origin, Bridge](
        cls,
        acknowledger: TurnInputAcknowledger[Origin, Bridge],
        thread: Thread,
        origins: tuple[Origin, ...],
    ) -> None:
        """Settle non-waking direct-inbox origins without starting a model turn."""
        del acknowledger, thread, origins

    async def context_for_input(self, thread, context):
        return context

    async def abort[Result](self, owner: asyncio.Task[Result]) -> None:
        """Backends without RunsTurns have no turn work to interrupt."""

    async def close_idle(self) -> None:
        """A backend without a retained process has nothing to retire."""

    @property
    def runtime_process(self) -> ProcessState:
        """The per-session runtime child, never the shared worker hosting this backend."""
        return NoProcess()

    async def inspect(self, thread: Thread, request: BackendQuery) -> BackendQueryResponse:
        """A runtime without native queries refuses this capability explicitly."""
        raise BackendDeclarationError(f"{type(self).__name__} does not support native inspection")

    @classmethod
    def served(cls) -> type[AgentBackend]:
        """The runtime-serving backend class for this declaration."""
        return cls

    def __init_subclass__(cls, **options: str | None) -> None:
        if AgentBackend._sealed:
            replaces = vars(cls).get(REPLACES_REGISTERED_ATTRIBUTE)
            if replaces not in dict.values(AgentBackend.__registry_config__.registry_dict):
                raise BackendDeclarationError(
                    f"{cls.__name__}: the AgentBackend registry is sealed; declare backends "
                    "before the Thread backend lookup seals its declarations."
                )
        super().__init_subclass__(**options)

    @classmethod
    def _served_declaration(cls, declaration: type[DeclaredFamily]) -> None:
        if not issubclass(cls.location_type, declaration.location_type):
            raise BackendDeclarationError(
                f"{cls.__name__} location {cls.location_type.__name__} does not serve "
                f"{declaration.location_type.__name__}"
            )
        cls._served_declarations = (declaration,)
        if AgentBackend._sealed:
            cls.location_type._backend = cls
            declaration.location_type._backend = cls

    @classmethod
    @abstractmethod
    def for_worker(cls, runner: TurnRunner, session_id: AcpSessionId) -> Self:
        """An instance holding this runtime for one thread session in this worker."""

    @classmethod
    @abstractmethod
    def new_location(cls, root, worktree) -> RuntimeLocation: ...

    @classmethod
    def unstarted_location(
        cls, fallback: Callable[[type[AgentBackend]], RuntimeLocation],
    ) -> RuntimeLocation:
        return fallback(cls)

    @classmethod
    def is_forkable(cls) -> bool:
        return False

    @classmethod
    def require_forkable(cls, parent: Thread) -> None:
        raise RelationViolationError(f"{cls.__name__} does not support forking.")

    @classmethod
    def join_forking_caller(cls, thread: Thread, join_child: Callable[[], None]) -> None:
        """Native Pi threads run under their own process rather than a shared MCP grant."""
        del join_child
        raise ToolRefusedError(
            f"Thread {thread.name!r} runs on Native Pi; "
            "call comms_cli_inbox first to take it over from a CLI/MCP session."
        )

    @classmethod
    def registered_over(cls, previous: type[AgentBackend]) -> type[AgentBackend]:
        """Keep a previous native backend across re-registration; adopt incoming when switching off CLI inbox."""
        return previous.as_native_backend(cls)

    @classmethod
    def as_native_backend(cls, incoming: type[AgentBackend]) -> type[AgentBackend]:
        return cls

    @classmethod
    def turn_transcript_sink(
        cls,
        root: Path,
        incarnation: ThreadIncarnation,
        turn_id: TurnId,
        opening: tuple[MessageReference, ...],
    ) -> TurnTranscriptSink:
        del root, incarnation, turn_id, opening
        return TurnTranscriptSink()

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
            for replaced in owner._served_declarations:
                replaced.location_type._backend = owner
        AgentBackend.__registry__ = MappingProxyType(  # type: ignore[assignment]
            AgentBackend.__registry_config__.registry_dict
        )
        AgentBackend._sealed = True

    def run_turn(self, thread: Thread, content: TurnInputContent, delivery: TurnDelivery):
        raise BackendDeclarationError(f"{type(self).__name__} does not run turns")

    @classmethod
    def require_event_family(cls, family: type[DeclaredFamily]) -> None:
        """Fail unless ``family`` converts exactly the event kinds this backend's runtime emits."""
        problems = _event_family_problems(cls, family)
        if problems:
            raise BackendDeclarationError(f"{cls.__name__}: " + "; ".join(problems))


class ConvertedEvent(FamilyMember):
    """A member of a converter family: it converts the runtime events of the
    kinds it names, by default the one its declared name spells."""

    @classmethod
    def wire_kind(cls) -> str:
        """The runtime event kind this member converts."""
        return cls.declared_name

    @classmethod
    def known_kinds(cls) -> tuple[str, ...]:
        return (cls.wire_kind(),)


class UnconvertedEvent(ConvertedEvent):
    """The member a converter family selects for a kind no member converts: it names none."""

    @classmethod
    def known_kinds(cls) -> tuple[str, ...]:
        return ()


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


class ServedRuntimeLocation(RuntimeLocation, metaclass=ServesDeclaration, declared_name=None):
    """Base for served RuntimeLocation classes replacing lightweight wire declarations."""


class DeclaredRuntimeLocation(RuntimeLocation, declared_name=None):
    """Lightweight RuntimeLocation declaration that acquires its served class on runtime access."""

    def _served(self) -> RuntimeLocation:
        return self.backend.served().location_type(self.path)

    def history_source(self, root, incarnation) -> TranscriptHistorySource:
        return self._served().history_source(root, incarnation)

    def storage_files(self):
        return self._served().storage_files()


class DeclaredNativeCutLocation(DeclaredRuntimeLocation, declared_name=None):
    """Lightweight RuntimeLocation declaration whose served class owns ``native_cut``."""

    def native_cut(self, key: str):
        return self._served().native_cut(key)


class DeclaredSavedModelLocation(DeclaredRuntimeLocation, declared_name=None):
    """Lightweight RuntimeLocation declaration whose served class reads ``saved_model``."""

    @property
    def saved_model(self) -> ThreadModel:
        return self._served().saved_model


class ExplicitlyRegisteredBackend(AgentBackend, declared_name=None):
    """Backend adopted explicitly on re-registration and yielded to an incoming native backend."""

    @classmethod
    def registered_over(cls, previous: type[AgentBackend]) -> type[AgentBackend]:
        del previous
        return cls

    @classmethod
    def as_native_backend(cls, incoming: type[AgentBackend]) -> type[AgentBackend]:
        return incoming


class ServedAgentBackend(AgentBackend, metaclass=ServesDeclaration, declared_name=None):
    """Base for served AgentBackend classes replacing lightweight wire declarations."""

    @classmethod
    def served(cls) -> type[AgentBackend]:
        return cls


class DeclaredAgentBackend(AgentBackend, declared_name=None):
    """Lightweight AgentBackend declaration; imports its runtime module only on execution."""

    runtime_module: ClassVar[str]

    @classmethod
    def served(cls) -> type[AgentBackend]:
        import_module_preserving_root_logging(cls.runtime_module)
        return AgentBackend.member_named(cls.declared_name)

    @classmethod
    def for_worker(cls, runner: TurnRunner, session_id: AcpSessionId):
        return cls.served().for_worker(runner, session_id)

    @classmethod
    def new_location(cls, root, worktree) -> RuntimeLocation:
        return cls.served().new_location(root, worktree)

    async def compact(self, thread: Thread, instructions: str | None) -> CompactionOutcome:
        raise BackendDeclarationError(
            f"{type(self).__name__} is a declaration; instantiate via {type(self).runtime_module}"
        )


class DeclaredForkingBackend(DeclaredAgentBackend, declared_name=None):
    """Lightweight backend declaration mixin with forking capability."""

    @classmethod
    async def fork(
        cls, parent: Thread, launcher: str, runtime: LaunchRuntime, root: Path,
    ) -> RuntimeLocation:
        return await cls.served().fork(parent, launcher, runtime, root)


class DeclaredThreadServingBackend(DeclaredAgentBackend, declared_name=None):
    """Lightweight thread-serving backend declaration mixin."""

    def run_turn(
        self, thread: Thread, content: TurnInputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        raise BackendDeclarationError(
            f"{type(self).__name__} is a declaration; instantiate via {type(self).runtime_module}"
        )

    async def abort[Result](self, owner: asyncio.Task[Result]) -> None:
        raise BackendDeclarationError(
            f"{type(self).__name__} is a declaration; instantiate via {type(self).runtime_module}"
        )

    async def close_idle(self) -> None:
        raise BackendDeclarationError(
            f"{type(self).__name__} is a declaration; instantiate via {type(self).runtime_module}"
        )
