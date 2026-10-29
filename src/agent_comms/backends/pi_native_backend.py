"""Pi over RPC, discovered when a runtime location or backend is requested."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from .. import backend
from ..agent_backend import (
    CompactionOutcome, FailedCompaction, Forks, InputContent, Interrupts,
    NewThreadBackend, NothingToCompaction, PlacedCompaction, RuntimeLocation,
)
from ..agent_events import AgentEvent, AgentInfo
from ..comms import Comms
from ..config_options import ConfigOptions
from ..coordinator import Coordination
from ..fresh_private_session import create_fresh_private_session
from ..launch_runtime import LaunchRuntime
from ..native_arguments import NativeArguments
from ..native_child import PersistentPiSession
from ..native_failures import NativePiUnavailable
from ..native_fork import RuntimeForkHelper, RuntimeForkRequest
from ..native_pi import NativePiRpcLaunch
from ..native_session_files import ForkedSession, NativeSessionFiles, NativeSessionLocks
from ..native_session_prepare import NativeSessionPreparation
from ..native_session_reopen import NativeSessionIdentity
from ..native_transcript import NativeTranscript
from ..owned_turn import TurnDelivery
from ..pi_commands import Compact
from ..pi_events import NativePiEvent
from ..pi_payloads import PiOmitted, StateData
from ..selected_session import SavedSelectedSession
from ..session_revision import SessionObservation, SessionRevision
from ..threads import Thread
from ..turn_runner import TurnRunner


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
                    NativeSessionIdentity.from_evidence(source_reads.for_source(path)))
        return SavedSelectedSession(directory, identity=identity,
                                    owner=participant.owner.thread.incarnation.require_recorded())

    def history_source(self, root, incarnation):
        return NativeTranscript(Path(self.path))

    def imported_sources(self):
        return NativeTranscript(Path(self.path)).imported_sources()

    def storage_files(self):
        return (NativeSessionLocks(Path(self.path)),)

    def session_observation(self, root: Path, thread: Thread) -> SessionObservation:
        return SessionRevision.observe(NativeSessionFiles.for_identity(
            NativeSessionIdentity.locate(self.path),
            root=root, owner=thread.incarnation.require_recorded()))

    @property
    def model_choice(self):
        for entry in NativeTranscript(Path(self.path)).tail():
            if entry.model_choice is not None:
                return entry.model_choice
        return None


class PiNativeBackend(PersistentPiSession, Interrupts, Forks, NewThreadBackend):
    """The session's backend composes the existing child lifecycle capabilities."""

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
    def for_worker(cls, runner: TurnRunner, session_id: str) -> Self:
        return cls(
            runner.inputs.state(session_id).owned().configuration, runner.comms,
            partial(runner.observe_selected_preparation, session_id),
        )

    @classmethod
    def new_location(cls, root, worktree) -> PiSessionFile:
        return PiSessionFile(str(create_fresh_private_session(root, worktree=worktree).path))

    def arguments(self, thread: Thread) -> tuple[str, ...]:
        return thread.model.native(self.configuration.agent_args).with_thinking(
            thread.thinking_level
        ).argv

    async def prepare(self, thread: Thread, *, open_native=NativeSessionPreparation.open) -> StateData:
        environment = await Coordination.run_worker(lambda: self.configuration.native_environment(
            thread, self.comms.registry.snapshot(),
        ))
        package = self.comms.owners.require_private_launch().native_package
        return await open_native(
            self,
            self.configuration.launch.binary,
            thread.native_launch_arguments(
                NativeArguments.parse(self.arguments(thread)), package.path, self.comms.root),
            package=package,
            worktree=thread.worktree.path,
            root=self.comms.root,
            owner=thread.incarnation.require_recorded(),
            session_directory=thread.native_session_directory(self.comms.root),
            environment=environment,
            session_file=thread.writable_runtime.read_key,
            observe=partial(self.observe, thread),
        )

    async def inspect(self, thread: Thread, request):
        return await self.child_state.inspect(self, partial(self.prepare, thread), request)

    async def run_turn(
        self, thread: Thread, content: InputContent, delivery: TurnDelivery
    ) -> AsyncIterator[AgentEvent]:
        images = {"images": content.images} if content.images else {}
        package = self.comms.owners.require_private_launch().native_package
        async for event in backend.stream_agent_events(
            self.configuration.launch.binary,
            thread.native_launch_arguments(
                NativeArguments.parse(self.arguments(thread)), package.path, self.comms.root),
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
        await backend.terminate_task_process(owner)

    async def compact(self, thread: Thread, instructions) -> CompactionOutcome:
        try:
            command = Compact(custom_instructions=PiOmitted.from_input(instructions))
            data = await self.inspect(thread, command)
        except NativePiUnavailable as error:
            return FailedCompaction(str(error))
        if data.summary is None:
            return NothingToCompaction()
        return PlacedCompaction(
            summary=data.summary,
            first_kept=data.first_kept_entry_id.value_or(None),
            tokens_before=data.tokens_before,
        )

    @classmethod
    async def fork(
        cls, parent: Thread, launcher: str, runtime: LaunchRuntime, root: Path,
    ) -> PiSessionFile:
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
