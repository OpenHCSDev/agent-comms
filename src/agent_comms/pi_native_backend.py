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
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.agent_backend import (
    CompactionOutcome,
    FailedCompaction,
    Forks,
    Interrupts,
    NewThreadBackend,
    NothingToCompaction,
    PlacedCompaction,
    RuntimeLocation,
)
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.fresh_private_session import create_fresh_private_session
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.native_arguments import NativeArguments
from agent_comms.execution.native_entries import NativeEvidenceScope
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_fork import RuntimeForkHelper, RuntimeForkRequest
from agent_comms.execution.native_pi import NativePiRpcLaunch
from agent_comms.execution.native_session_files import (
    ForkedSession,
    NativeSessionFiles,
    NativeSessionLocks,
)
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.persistent_pi_session import (
    PersistentPiSession as PersistentPiSession,
)
from agent_comms.execution.transcript_sources import NativeTranscript
from agent_comms.execution.pi_commands import Compact, NativeQuery
from agent_comms.execution.pi_events import NativePiEvent
from agent_comms.execution.pi_wire import PiOmitted
from agent_comms.execution.selected.session import SavedSelectedSession
from agent_comms.execution.session_revision import SessionObservation, SessionRevision
from agent_comms.execution.session_revision import (
    SessionObservation,
    SessionRevision,
    SessionRevisionUnavailable,
)
from agent_comms.execution.turn_inbox import InputContent
from agent_comms.installer.native_package import SYSTEM_PROMPT
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.pi_payloads import PiResponseData
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel
from agent_comms.team.rule_deal import SystemPromptDelivery

if TYPE_CHECKING:
    from agent_comms.agent_events import AgentInfo
    from agent_comms.comms import Comms
    from agent_comms.config_options import ConfigOptions
    from agent_comms.execution.owned_turn import TurnDelivery
    from agent_comms.execution.turn_runner import TurnRunner
    from agent_comms.pi_payloads import StateData
    from agent_comms.registry.threads import Thread


# The event kinds Pi 0.85.1 writes to RPC stdout, as built for Core. Sources,
# all in @earendil-works/pi-coding-agent 0.85.1 as patched by stack/:
#   dist/core/agent-session.d.ts   AgentSessionEvent union
#   pi-agent-core dist/types.d.ts   AgentEvent union (agent_start .. tool_execution_end)
#   dist/modes/rpc/rpc-mode.js      output(): response, extension_ui_request, extension_error
#   stack/native/patch-native-steering.py  steering_interrupt_started / _completed
PI_0_85_1_EVENT_KINDS: frozenset[str] = frozenset({
    "agent_start", "agent_end", "agent_settled", "native_operation_error",
    "turn_start", "turn_end",
    "message_start", "message_update", "message_end", "native_output_committed",
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

    def select_session(self, directory, execution, source_reads, *, participant):
        path = Path(self.path).absolute()
        retained = execution.retained_source(path)
        identity = (retained.identity if retained is not None else
                    source_reads.for_source(path).session_identity())
        return SavedSelectedSession(directory, identity=identity,
                                    owner=participant.owner.thread.incarnation.require_recorded())

    def history_source(self, root, incarnation):
        return NativeTranscript(Path(self.path))

    def deal_delivery(self) -> SystemPromptDelivery:
        """Native Pi always launches with the prepared prompt (NativePiRpcLaunch.bootstrap)."""
        return SystemPromptDelivery(SYSTEM_PROMPT)

    def imported_sources(self):
        return NativeTranscript(Path(self.path)).imported_sources()

    def storage_files(self):
        return (NativeSessionLocks(Path(self.path)),)

    def session_observation(self, root: Path, thread: Thread) -> SessionObservation:
        return SessionRevision.observe(NativeSessionFiles.for_identity(
            NativeEvidenceScope.locate_session(self.path),
            root=root, owner=thread.incarnation.require_recorded()))

    @property
    def saved_model(self) -> ThreadModel:
        """The newest model the saved session selected, read from its tail."""
        return next((model for entry in NativeTranscript(Path(self.path)).tail()
                     for model in entry.saved_models), UndeclaredModel())


class PiNativeBackend(
    PersistentPiSession,
    Interrupts,
    Forks,
    NewThreadBackend,
):
    """Pi 0.85.1 over RPC: one retained child per thread session in this worker.

    The instance owns the session's child. Its launch command (``agent_bin``
    plus ``agent_args``) and the publisher of attested session info are fixed
    when the worker creates it.
    """

    location_type: ClassVar[type[RuntimeLocation]] = PiSessionFile
    event_kinds: ClassVar[frozenset[str]] = PI_0_85_1_EVENT_KINDS

    def __init__(
        self,
        configuration: ConfigOptions,
        comms: Comms,
        observe: Callable[[Thread, StateData, AgentInfo], Awaitable[None]],
    ) -> None:
        super().__init__()
        self.configuration, self.comms = configuration, comms
        self.observe = observe

    @classmethod
    def for_worker(cls, runner: TurnRunner, session_id: AcpSessionId) -> Self:
        return cls(
            runner.inputs.state(session_id).owned().configuration, runner.comms,
            partial(runner.observe_selected_preparation, session_id),
        )

    @classmethod
    def new_location(cls, root, worktree) -> PiSessionFile:
        return PiSessionFile(str(create_fresh_private_session(root, worktree=worktree).path))

    def arguments(self, thread: Thread) -> tuple[str, ...]:
        """Pi's command-line arguments for this thread's model and thinking level."""
        return thread.model.native(self.configuration.agent_args).with_thinking(
            thread.thinking_level
        ).argv

    # -- lifecycle --------------------------------------------------------

    async def prepare(self, thread: Thread, *, open_native=None) -> StateData:
        """Launch (or reuse) and attest the idle child for ``thread``'s saved session."""
        from agent_comms.execution.native_session_prepare import (
            NativeSessionPreparation,
        )

        if open_native is None:
            open_native = NativeSessionPreparation.open
        environment = await Coordination.run_worker(lambda: self.configuration.native_environment(
            thread, self.comms.registry.snapshot(),
        ))
        package = self.comms.owners.require_private_launch().native_package
        return await open_native(
            self,
            self.configuration.launch.binary,
            self.comms.threads.native_launch_arguments(
                thread, NativeArguments.parse(self.arguments(thread)), package.path),
            package=package,
            worktree=thread.worktree.path,
            root=self.comms.root,
            owner=thread.incarnation.require_recorded(),
            session_directory=thread.native_session_directory(self.comms.root),
            environment=environment,
            session_file=thread.writable_runtime.read_key,
            observe=partial(self.observe, thread),
        )

    async def inspect(self, thread: Thread, request: NativeQuery) -> PiResponseData:
        """Answer one Pi query on the idle child, preparing it first when needed."""
        return await self.child_state.inspect(self, partial(self.prepare, thread), request)

    # -- RunsTurns / Steers / Interrupts ------------------------------------

    async def run_turn(
        self, thread: Thread, content: InputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        from agent_comms.execution import backend

        images = {"images": content.images} if content.images else {}
        package = self.comms.owners.require_private_launch().native_package
        async for event in backend.stream_agent_events(
            self.configuration.launch.binary,
            self.comms.threads.native_launch_arguments(
                thread, NativeArguments.parse(self.arguments(thread)), package.path),
            content.text,
            delivery.worktree,
            delivery.environment,
            **images,
            context_contributions=content.contributions,
            **thread.writable_runtime.arguments(),
            steering_queue=delivery.inbox,
            finish_event=delivery.finish_event,
            send_boundary=delivery.send_boundary,
            input_start=delivery.input_start,
            cursor_delivery=delivery.cursor_delivery,
            request_observer=delivery.request_progress,
            persistent_session=self,
            package=package,
            root=self.comms.root,
            owner=thread.incarnation.require_recorded(),
            session_directory=thread.native_session_directory(self.comms.root),
            ui_request=delivery.ui_request,
            tool_mode=delivery.tool_mode,
        ):
            yield event

    async def abort[Result](self, owner: asyncio.Task[Result]) -> None:
        from agent_comms.execution import backend

        await backend.terminate_task_process(owner)

    # -- Compacts ---------------------------------------------------------

    async def compact(self, thread: Thread, instructions: str | None) -> CompactionOutcome:
        """Ask Pi's own engine to compact through its ``compact`` RPC."""
        try:
            command = Compact(custom_instructions=PiOmitted.from_input(instructions))
            data = (await self.inspect(thread, command)).require_compaction()
        except NativePiUnavailable as error:
            return FailedCompaction(str(error))
        return next((PlacedCompaction(
            summary=summary,
            tokens_before=data.tokens_before,
        ) for summary in data.summary.present()), NothingToCompaction())

    # -- Forks ------------------------------------------------------------

    @classmethod
    async def fork(
        cls, parent: Thread, launcher: str, runtime: LaunchRuntime, root: Path,
    ) -> PiSessionFile:
        """Copy the parent's whole saved history with Pi's own session manager."""
        package = await Coordination.run_worker(partial(
            NativePiRpcLaunch.package_for_command, launcher, runtime,
        ))
        directory = await Coordination.run_worker(partial(ForkedSession.prepared_directory, root))
        identity = await RuntimeForkHelper.run(
            RuntimeForkRequest(str(package.path), parent.runtime.path, str(parent.worktree),
                               str(directory), runtime=runtime), cwd=parent.worktree.path,
        )
        return PiSessionFile(identity.session_file)


PiNativeBackend.require_event_family(NativePiEvent)
