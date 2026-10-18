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
    Forks,
    InputContent,
    Interrupts,
    RuntimeLocation,
    TurnDelivery,
)
from .fresh_private_session import create_fresh_private_session
from .native_pi import NativePiRpcLaunch
from .native_session_files import NativeSessionFiles
from .native_session_reopen import SessionIdentityHelper
from .native_transcript import NativeTranscript
from .pi_capabilities import PiCapabilities
from .selected_session import SavedSelectedSession
from .unstarted_runtime import UnstartedRuntime

if TYPE_CHECKING:
    from .agent_events import AgentEvent, AgentInfo
    from .comms import Comms
    from .launch_runtime import LaunchRuntime
    from .native_arguments import NativeArguments
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
        retained = execution.backend.retained_source(path, execution.native_package)
        identity = (retained.identity if retained is not None else
                    SessionIdentityHelper.locate(execution.native_package, str(path)))
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

    async def run_turn(
        self, thread: Thread, content: InputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        from .agent_events import AgentEvent
        from .diagnostics import record_request_progress
        from .maintenance_barrier import MaintenanceBarrier
        from .native_startup import NativeStartup
        from .selected_turn_delivery import SelectedTurnDelivery
        from .tracked_turn import TrackedDone, TrackedTurnSession

        if not isinstance(delivery, SelectedTurnDelivery):
            async for event in self.stream_native_turn(thread, content, delivery):
                yield event
            return

        claim = delivery.claim
        # TrackedTurnSession is the native turn owner. Adapt its callback to the
        # backend event stream without letting it outrun the selected observer.
        pending: asyncio.Queue[tuple[AgentEvent, asyncio.Future[None]] | None] = asyncio.Queue()

        async def observe(event):
            if not isinstance(event, AgentEvent):
                await delivery.observe_event(event)
                return
            acknowledged = asyncio.get_running_loop().create_future()
            await pending.put((event, acknowledged))
            await acknowledged

        async def produce():
            try:
                result = await TrackedTurnSession.execute(
                    delivery.execution.native_package,
                    input_id=claim.input_id,
                    prompt=content.text,
                    context_contributions=content.contributions,
                    worktree=Path(thread.worktree).absolute(),
                    session=claim.session,
                    model=delivery.model,
                    thinking_level=thread.thinking_level,
                    environment=delivery.environment,
                    ingress=MaintenanceBarrier(claim.bus._registry.store.path),
                    startup=NativeStartup(claim.bus.log.path.parent, measurements=claim._measurements),
                    selected_tool_mode=delivery.selected_tool_mode,
                    observe_event=observe,
                    request_observer=partial(record_request_progress, claim.bus.log.path.parent,
                                             thread.require_turn_lease()),
                    prompt_send_boundary=claim,
                    execution=delivery.execution,
                    following=delivery.following,
                )
                await observe(TrackedDone(text=result.text, ok=True, proof=result.context))
            finally:
                pending.put_nowait(None)

        task = asyncio.create_task(produce())
        try:
            while (item := await pending.get()) is not None:
                event, acknowledged = item
                try:
                    yield event
                finally:
                    if not acknowledged.done():
                        acknowledged.set_result(None)
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def retained_source(self, session_file: Path, package: Path):
        from .native_pi import NativePiUnavailable

        try:
            retained = self.child_state.idle()
        except NativePiUnavailable:
            return None
        if (retained.identity.session_file != str(session_file)
                or retained.child.key[0].package != package):
            return None
        return retained

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
    PiCapabilities,
    Forks,
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
    def registered_location(cls, root, worktree) -> PiSessionFile:
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
        return await open_native(
            self,
            self.agent_bin,
            self.arguments(thread),
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

    async def stream_native_turn(
        self, thread: Thread, content: InputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        from . import backend

        images = {"images": content.images} if content.images else {}
        async for event in backend.stream_agent_events(
            self.agent_bin,
            self.arguments(thread),
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
            root=self.comms.root,
            ui_request=delivery.ui_request,
        ):
            yield event

    async def abort(self, owner: asyncio.Task[Any]) -> None:
        from . import backend

        await backend.terminate_task_process(owner)

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
            RuntimeForkRequest(str(package), parent.runtime.path, parent.worktree,
                               runtime=runtime), cwd=Path(parent.worktree),
        )
        return PiSessionFile(identity.session_file)

    # -- ConfiguresModel --------------------------------------------------

    async def discover_models(self, thread: Thread):
        from .pi_commands import GetAvailableModels

        return await GetAvailableModels().discover(self.agent_bin, self.agent_args.argv)

    async def apply_settings(self, thread, commands):
        arguments = self.arguments(thread)
        for command in commands:
            state = await command.configure(self.agent_bin, arguments, worktree=Path(thread.worktree))
            arguments = self.agent_args.with_model(state.model.display_name).with_thinking(
                state.thinking_level
            ).argv
        return state
