"""The Pi 0.85.1 runtime as an AgentBackend: one retained Pi RPC child per thread session.

``PiNativeBackend`` is the only entry point Core uses for this runtime. It owns
the per-session child (``PersistentPiSession``), its launch command, and the
conversions from Pi's RPC, session file and settings vocabulary to Core's
records. The transport, child lifecycle and turn machinery stay in private
implementation modules (``backend``, ``pi_commands``, ``pi_events``,
``native_entries``) and are imported lazily: this module is declared while
``threads`` is still importing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from .agent_backend import (
    AgentBackend,
    CompactionOutcome,
    Compacts,
    ConfiguresModel,
    FailedCompaction,
    Forks,
    InputContent,
    Interrupts,
    NothingToCompaction,
    PlacedCompaction,
    RuntimeLocation,
    TurnDelivery,
)
from .fresh_private_session import create_fresh_private_session
from .native_arguments import NativeArguments
from .native_pi import NativePiRpcLaunch
from .native_session_files import NativeSessionFiles
from .native_session_reopen import NativeSessionIdentity
from .native_transcript import NativeTranscript
from .selected_session import SavedSelectedSession
from .unstarted_runtime import UnstartedRuntime

if TYPE_CHECKING:
    from .agent_events import AgentEvent, AgentInfo
    from .comms import Comms
    from .launch_runtime import LaunchRuntime
    from .native_child import NativeChildState, PiSessionChild
    from .native_session_reopen import NativeSessionIdentity
    from .pi_payloads import StateData
    from .threads import Thread


# The event kinds Pi 0.85.1 writes to RPC stdout, as built for Core. Sources,
# all in @earendil-works/pi-coding-agent 0.85.1 as patched by stack/:
#   dist/core/agent-session.d.ts   AgentSessionEvent union
#   pi-agent-core dist/types.d.ts   AgentEvent union (agent_start .. tool_execution_end)
#   dist/modes/rpc/rpc-mode.js      output(): response, extension_ui_request, extension_error
#   stack/patch-native-steering.py  steering_interrupt_started / _completed
PI_0_85_1_EVENT_KINDS: frozenset[str] = frozenset({
    "agent_start", "agent_end", "agent_settled",
    "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
    "input_committed", "context_committed", "turn_context_observed", "model_request_progress",
    "queue_update", "entry_appended", "session_info_changed", "thinking_level_changed",
    "bash_execution_update",
    "compaction_start", "compaction_end",
    "auto_retry_start", "auto_retry_end",
    "native_retry_start", "native_retry_end",
    "summarization_retry_scheduled", "summarization_retry_attempt_start",
    "summarization_retry_finished",
    "steering_interrupt_started", "steering_interrupt_completed",
    "response", "extension_ui_request", "extension_error",
})


@dataclass(frozen=True)
class PiSessionFile(RuntimeLocation):
    """The native backend's stored Pi transcript."""

    path: str

    def select_session(self, directory, execution):
        path = Path(self.path).absolute()
        retained = execution.retained_source(path)
        identity = (retained.identity if retained is not None else
                    NativeSessionIdentity.from_evidence(
                        execution.cursor_delivery.source_reads.for_source(path)
                    ))
        return SavedSelectedSession(directory, identity=identity)

    def history_source(self, root, incarnation):
        return NativeTranscript(Path(self.path))

    def imported_sources(self):
        return NativeTranscript(Path(self.path)).imported_sources()

    def storage_files(self):
        return (NativeSessionFiles(Path(self.path)),)

    @property
    def model_choice(self):
        for entry in NativeTranscript(Path(self.path)).tail():
            if entry.model_choice is not None:
                return entry.model_choice
        return None


class PersistentPiSession:
    """Serialize borrowing and retirement of one native session's actual child."""

    def __init__(self) -> None:
        from .native_child import EmptyNative

        self.lock = asyncio.Lock()
        self.child_state: NativeChildState = EmptyNative()

    @property
    def available(self) -> bool:
        return self.child_state.available

    async def open(self, launch, *, reuse, startup, watchdog) -> PiSessionChild:
        from .native_child import BorrowedNative, EmptyNative, PiSessionChild

        key = (launch, launch.configuration.auth_revision())
        child = self.child_state.reuse(key) if reuse else None
        reused = child is not None
        if child is None:
            await self.close()
            attestation = launch.session.attestation()
            watchdog.launching(asyncio.get_running_loop().time, launch.session.path)
            with startup.measurements.operation("native_spawn"):
                child = await PiSessionChild.start(key, attestation)
            self.child_state = BorrowedNative(child, self.child_state)
        else:
            watchdog.launching(asyncio.get_running_loop().time, launch.session.path)
            self.child_state = BorrowedNative(child, EmptyNative())
        watchdog.spawned(reused)
        return child

    async def context_for_input(self, thread, context):
        async with self.lock:
            return await self.child_state.context_for_input(thread, context)

    def retain(self, child: PiSessionChild, identity: NativeSessionIdentity) -> bool:
        from .native_child import RetainedNative
        from .session_revision import SessionRevision, SessionRevisionUnavailable

        if not child.proc.alive():
            return False
        try:
            revision = SessionRevision.observe(identity.session_file).require_available()
        except SessionRevisionUnavailable:
            return False
        self.child_state = RetainedNative(child, identity, revision)
        return True

    async def close(self) -> None:
        await self._finish_retirement(self.child_state.retire())

    async def _finish_retirement(self, retiring: NativeChildState) -> None:
        self.child_state = retiring
        try:
            await retiring.closed()
        finally:
            self.child_state = retiring.settled

    async def close_idle(self) -> None:
        async with self.lock:
            await self.close()


class PiNativeBackend(
    PersistentPiSession,
    Interrupts,
    Compacts,
    Forks,
    ConfiguresModel,
    AgentBackend,
):
    """Pi 0.85.1 over RPC: one retained child per thread session in this worker.

    The instance owns the session's child. Its launch command (``agent_bin``
    plus ``agent_args``) and the publisher of attested session info are fixed
    when the worker creates it.
    """

    location_type: ClassVar[type[RuntimeLocation]] = PiSessionFile
    unstarted_type: ClassVar[type[RuntimeLocation]] = UnstartedRuntime
    serves_new_threads: ClassVar[bool] = True
    event_kinds: ClassVar[frozenset[str]] = PI_0_85_1_EVENT_KINDS

    def __init__(
        self,
        agent_bin: str,
        agent_args: NativeArguments,
        comms: Comms,
        observe: Callable[[Thread, StateData, AgentInfo], Awaitable[None]],
    ) -> None:
        super().__init__()
        self.agent_bin, self.agent_args, self.comms = agent_bin, agent_args, comms
        self.observe = observe

    @classmethod
    def for_worker(cls, runner: Any, session_id: str) -> Self:
        return cls(
            runner.agent_bin, runner.agent_args, runner.comms,
            partial(runner.observe_selected_preparation, session_id),
        )

    @classmethod
    def new_location(cls, root, worktree) -> PiSessionFile:
        return PiSessionFile(str(create_fresh_private_session(root / "sessions", worktree=worktree).path))

    def arguments(self, thread: Thread) -> tuple[str, ...]:
        """Pi's command-line arguments for this thread's model and thinking level."""
        return self.agent_args.with_model(thread.model).with_thinking(
            thread.thinking_level
        ).argv

    # -- lifecycle --------------------------------------------------------

    async def prepare(self, thread: Thread, *, open_native=None) -> StateData:
        """Launch (or reuse) and attest the idle child for ``thread``'s saved session."""
        from .coordinator import Coordination
        from .native_session_prepare import NativeSessionPreparation

        if open_native is None:
            open_native = NativeSessionPreparation.open
        environment = await Coordination.run_worker(lambda: thread.native_environment(
            self.comms.root, self.comms.registry.snapshot(), thread.worktree,
        ))
        package = self.comms.owners.require_private_launch().native_package
        return await open_native(
            self,
            self.agent_bin,
            thread.native_launch_arguments(
                NativeArguments.parse(self.arguments(thread)), package.path),
            package=package,
            worktree=thread.worktree,
            root=self.comms.root,
            environment=environment,
            session_file=thread.runtime.read_key,
            observe=partial(self.observe, thread),
        )

    async def inspect(self, thread: Thread, request):
        """Answer one Pi query on the idle child, preparing it first when needed."""
        return await self.child_state.inspect(self, partial(self.prepare, thread), request)

    # -- RunsTurns / Steers / Interrupts ------------------------------------

    async def run_turn(
        self, thread: Thread, content: InputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        from . import backend

        images = {"images": content.images} if content.images else {}
        package = self.comms.owners.require_private_launch().native_package
        async for event in backend.stream_agent_events(
            self.agent_bin,
            thread.native_launch_arguments(
                NativeArguments.parse(self.arguments(thread)), package.path),
            content.text,
            delivery.worktree,
            delivery.environment,
            **images,
            context_contributions=content.contributions,
            **thread.runtime.arguments(),
            steering_queue=delivery.inbox,
            finish_event=delivery.finish_event,
            send_boundary=delivery.send_boundary,
            interrupt_boundary=delivery.interrupt_boundary,
            native_start=delivery.input_started,
            request_observer=delivery.request_progress,
            persistent_session=self,
            package=package,
            root=self.comms.root,
            ui_request=delivery.ui_request,
            tool_mode=delivery.tool_mode,
        ):
            yield event

    async def abort(self, owner: asyncio.Task[Any]) -> None:
        from . import backend

        await backend.terminate_task_process(owner)

    # -- Compacts ---------------------------------------------------------

    async def compact(self, thread: Thread, instructions: str | None) -> CompactionOutcome:
        """Ask Pi's own engine to compact through its ``compact`` RPC."""
        from .native_pi import NativePiUnavailable
        from .pi_commands import Compact

        try:
            data = await self.inspect(thread, Compact(custom_instructions=instructions))
        except (NativePiUnavailable, ValueError) as error:
            return FailedCompaction(str(error))
        if data.summary is None:
            return NothingToCompaction()
        return PlacedCompaction(
            summary=data.summary,
            first_kept=data.first_kept_entry_id,
            tokens_before=data.tokens_before,
        )

    # -- Forks ------------------------------------------------------------

    @classmethod
    async def fork(cls, parent: Thread, launcher: str, runtime: LaunchRuntime) -> PiSessionFile:
        """Copy the parent's whole saved history with Pi's own session manager."""
        from .coordinator import Coordination
        from .native_fork import RuntimeForkHelper, RuntimeForkRequest

        package = await Coordination.run_worker(partial(
            NativePiRpcLaunch.package_for_command, launcher, runtime,
        ))
        identity = await RuntimeForkHelper.run(
            RuntimeForkRequest(str(package.path), parent.runtime.path, parent.worktree,
                               runtime=runtime), cwd=Path(parent.worktree),
        )
        return PiSessionFile(identity.session_file)

    # -- ConfiguresModel --------------------------------------------------

    async def models(self, thread: Thread) -> tuple[str, ...]:
        from .pi_commands import GetAvailableModels

        data = await GetAvailableModels().discover(self.agent_bin, self.agent_args.argv,
            package=self.comms.owners.require_private_launch().native_package)
        return tuple(dict.fromkeys(
            model.display_name for model in data.models if model.provider and model.id
        ))

    async def configure(self, thread: Thread, model: str | None, thinking: str | None) -> tuple[str, str]:
        from .native_pi import NativePiUnavailable
        from .pi_commands import SetModel, SetThinkingLevel

        commands = []
        if model is not None:
            commands.append(SetModel.from_name(model))
        if thinking is not None:
            commands.append(SetThinkingLevel(level=thinking))
        if not commands:
            raise ValueError("A configuration change names a model or a thinking level")
        arguments = self.arguments(thread)
        for command in commands:
            state = await command.configure(self.agent_bin, arguments,
                package=self.comms.owners.require_private_launch().native_package,
                worktree=Path(thread.worktree))
            arguments = self.agent_args.with_model(state.model.display_name).with_thinking(
                state.thinking_level
            ).argv
        selected, level = state.model.display_name, state.thinking_level.wire_value()
        if selected is None or level is None:
            raise NativePiUnavailable("Native configuration did not report model and thinking level")
        return selected, level
