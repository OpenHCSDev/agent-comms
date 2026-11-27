"""Antigravity ACP runtime as a first-class ``AgentBackend``.

All Antigravity threads inside a worker share ONE multiplexed ``agy acp`` process
through ``AntigravityAcpPool``, giving every Antigravity thread native ``agent-comms``
semantics — ``new``, ``start``, ``stop``, ``fork`` (WAL-safe SQLite clone +
``trajectory_meta`` reassignment), ``configure`` (model + thinking level), ``compact``,
and ``abort`` — with zero per-thread TUI processes and zero tmux poking.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms import agent_events as events
from agent_comms.agent_backend import (
    AgentBackend,
    CompactionOutcome,
    FailedCompaction,
    Forks,
    PlacedCompaction,
    RuntimeLocation,
    ThreadServingBackend,
)
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.installer.harness_install import AntigravityAcpSession
from agent_comms.execution.antigravity_acp import (
    AcpRpcError,
    AntigravityAcpPool,
    AntigravityAcpProcess,
    AntigravityConversationId,
    AntigravitySessionFiles,
    AntigravitySessionStorage,
)
from agent_comms.execution.antigravity_acp_events import (
    ANTIGRAVITY_ACP_EVENT_KINDS,
    AcpConfigKey,
    AcpTurnStreamState,
    AntigravityAcpUpdate,
)
from agent_comms.execution.backend_query import TurnInputContent
from agent_comms.execution.child_process import ProcessState
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.native_arguments import AntigravityProvider
from agent_comms.execution.runtime_observations import (
    AgentInfo,
    ReportedNativeSessionPath,
)
from agent_comms.execution.turn_failure import BackendFailed
from agent_comms.execution.turn_phase import (
    NativePhaseChanged,
    SettlingStatsPhase,
    TurnPhase,
)
from agent_comms.execution.unstarted_runtime import ExternalTranscriptLocation
from agent_comms.nominal.field_codec import (
    AcpSessionId,
    AcpSessionSelection,
    InputId,
    NoAcpSessionId,
)
from agent_comms.pi_counts import UnreportedCount
from agent_comms.registry.thread_model import DeclaredModel, ThreadModel, UndeclaredModel

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.config_options import ConfigOptions
    from agent_comms.execution.input_drain import InputDrain
    from agent_comms.execution.owned_turn import TurnDelivery
    from agent_comms.execution.pi_commands import SettingCommand
    from agent_comms.execution.session_config_options import SessionConfigOptions
    from agent_comms.messaging.messages import Message
    from agent_comms.registry.threads import Thread


@dataclass(frozen=True)
class AntigravitySession(ExternalTranscriptLocation, declared_name="antigravity_session"):
    """On-disk Antigravity conversation database (``conversations/<uuid>.db``)."""

    path: str

    @classmethod
    def fresh(cls) -> Self:
        storage = AntigravitySessionStorage.for_id(AntigravityConversationId(str(uuid.uuid4())))
        return cls(str(storage.db_path))

    @classmethod
    def fresh_in(cls, home: Path) -> Self:
        storage = AntigravitySessionStorage.for_home(
            AntigravityConversationId(str(uuid.uuid4())), home
        )
        return cls(str(storage.db_path))

    @property
    def storage(self) -> AntigravitySessionStorage:
        return AntigravitySessionStorage.from_db_path(Path(self.path))

    def last_written(self) -> float:
        return self.storage.last_written()

    def storage_files(self) -> tuple[AntigravitySessionFiles, ...]:
        return (AntigravitySessionFiles.for_storage(self.storage),)

    @property
    def saved_model(self) -> ThreadModel:
        observed = self.storage.latest_reported_model()
        if not observed:
            return UndeclaredModel()
        if "/" in observed:
            return DeclaredModel(observed)
        return DeclaredModel(f"antigravity/{observed}")


def _selected_model_name(*, provider: object = "", model_id: object = "") -> str:
    del provider
    return str(model_id)


class AntigravityAcpBackend(Forks, ThreadServingBackend, declared_name="antigravity_acp"):
    """Antigravity over ACP: concurrent thread sessions multiplexed on one ``agy acp`` server."""

    location_type: ClassVar[type[RuntimeLocation]] = AntigravitySession
    event_kinds: ClassVar[frozenset[str]] = ANTIGRAVITY_ACP_EVENT_KINDS
    provider_type: ClassVar[type[AntigravityProvider]] = AntigravityProvider

    @classmethod
    def registered_over(cls, previous: type[AgentBackend]) -> type[AgentBackend]:
        del previous
        return cls

    @classmethod
    def as_native_backend(cls, incoming: type[AgentBackend]) -> type[AgentBackend]:
        return incoming

    @classmethod
    async def apply_setting(
        cls,
        owner: SessionConfigOptions,
        configuration: ConfigOptions,
        thread: Thread,
        command: SettingCommand,
    ) -> None:
        del configuration
        await owner.store_direct_setting(thread, command)

    @classmethod
    async def drain_owned_inbox(
        cls,
        inputs: InputDrain,
        session_id: AcpSessionId,
        thread: Thread,
    ) -> None:
        await inputs.drain_direct_inbox(session_id, thread)

    @classmethod
    def acknowledge_turn_inputs(
        cls,
        comms: Comms,
        thread: Thread,
        origins: tuple[Message, ...],
    ) -> None:
        comms.messaging.acknowledge_direct_turn_inputs(
            thread,
            origins,
            root_id=comms.owners.require_private_launch().wire_root_id,
            bridge=AntigravityAcpSession,
        )

    def __init__(self, process: AntigravityAcpProcess) -> None:
        self._process = process
        self._acp_session: AcpSessionSelection = NoAcpSessionId()
        self._applied_model: str = ""
        self._applied_thinking: str = ""
        self._lock = asyncio.Lock()

    @classmethod
    def for_worker(cls, runner: object, session_id: AcpSessionId) -> Self:
        del session_id
        return cls(AntigravityAcpPool.for_runner(runner))

    @classmethod
    def new_location(cls, root: Path, worktree: Path) -> AntigravitySession:
        del root, worktree
        return AntigravitySession.fresh()

    @classmethod
    async def fork(
        cls,
        parent: Thread,
        launcher: str,
        runtime: LaunchRuntime,
        root: Path,
    ) -> AntigravitySession:
        del launcher, runtime, root
        source = AntigravitySessionStorage.from_db_path(Path(parent.runtime.path))
        cloned = await asyncio.to_thread(
            source.clone_to, AntigravityConversationId(str(uuid.uuid4()))
        )
        return AntigravitySession(str(cloned.db_path))

    @property
    def runtime_process(self) -> ProcessState:
        return self._process.process_identity

    def _remember_applied(self, state: AcpTurnStreamState) -> None:
        if state.current_model:
            self._applied_model = state.current_model
        if state.thought_level:
            self._applied_thinking = state.thought_level

    async def _ensure_acp_session(
        self,
        thread: Thread,
        *,
        worktree: Path,
    ) -> tuple[AcpSessionId, bool, Path]:
        storage = AntigravitySessionStorage.from_db_path(Path(thread.writable_runtime.path))
        bound_sessions = self._acp_session.selected((), lambda sid: (sid,))
        if bound_sessions and self._process.alive:
            return bound_sessions[0], False, storage.db_path
        if storage.exists:
            descriptor = await self._process.resume_session(
                AcpSessionId(str(storage.conversation_id)), worktree
            )
            self._acp_session = descriptor.session_id
            self._applied_model = descriptor.current_model
            self._applied_thinking = descriptor.thought_level
            return descriptor.session_id, False, storage.db_path
        descriptor = await self._process.new_session(worktree)
        self._acp_session = descriptor.session_id
        self._applied_model = descriptor.current_model
        self._applied_thinking = descriptor.thought_level
        actual = AntigravitySessionStorage(
            root=storage.root,
            conversation_id=AntigravityConversationId(str(descriptor.session_id)),
        )
        return descriptor.session_id, True, actual.db_path

    async def _sync_configuration(self, session_id: AcpSessionId, thread: Thread) -> None:
        wanted_model = thread.model.set_model(_selected_model_name)
        if wanted_model and wanted_model != self._applied_model:
            await self._process.set_config_option(session_id, AcpConfigKey("model"), wanted_model)
            self._applied_model = wanted_model
        wanted_thinking = thread.thinking_level.wire_value()
        if wanted_thinking and wanted_thinking != self._applied_thinking:
            await self._process.set_config_option(
                session_id, AcpConfigKey("thought_level"), wanted_thinking
            )
            self._applied_thinking = wanted_thinking

    async def run_turn(
        self,
        thread: Thread,
        content: TurnInputContent,
        delivery: TurnDelivery,
    ) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            native_input_id = InputId.mint()
            with delivery.send_boundary.send_original(native_input_id, content.text) as outcome:
                refusal = outcome.run_direct(
                    on_granted=lambda: (
                        delivery.input_start.original(native_input_id, content.text),
                        "",
                    )[1],
                    on_denied=lambda reason: reason,
                )
            if refusal:
                yield events.Done(text=refusal, ok=False)
                return
            yield events.PromptAccepted()
            for started_event in delivery.input_start.started_events:
                yield started_event

            try:
                session_id, created, db_path = await self._ensure_acp_session(
                    thread,
                    worktree=Path(delivery.worktree),
                )
                if created:
                    yield AgentInfo(
                        model=thread.model,
                        thinking_level=thread.thinking_level,
                        session_file=ReportedNativeSessionPath(str(db_path)),
                    )
                await self._sync_configuration(session_id, thread)
                state = AcpTurnStreamState(
                    current_model=self._applied_model,
                    thought_level=self._applied_thinking,
                )
                last_phase: TurnPhase = state.phase
                yield NativePhaseChanged(last_phase, self._process.process_identity)
                async for frame in self._process.prompt(session_id, content.acp_prompt_blocks()):
                    for event in frame.apply(state, self._remember_applied):
                        yield event
                    if state.phase != last_phase:
                        last_phase = state.phase
                        yield NativePhaseChanged(last_phase, self._process.process_identity)
                yield NativePhaseChanged(SettlingStatsPhase(), self._process.process_identity)
                yield events.StreamSettled()
                yield events.Done(text=state.full_text, ok=True)
            except (AcpRpcError, ConnectionError, TimeoutError) as error:
                message = str(error)
                yield events.Error(text=message)
                yield events.Done(text=message, ok=False, failure=BackendFailed(message))

    async def abort[Result](self, owner: asyncio.Task[Result]) -> None:
        del owner
        for active in self._acp_session.selected((), lambda sid: (sid,)):
            with suppress(AcpRpcError):
                await self._process.cancel(active)

    async def close_idle(self) -> None:
        async with self._lock:
            bound_sessions = self._acp_session.selected((), lambda sid: (sid,))
            self._acp_session = NoAcpSessionId()
            self._applied_model = ""
            self._applied_thinking = ""
            for active in bound_sessions:
                await self._process.close_session(active)

    async def compact(self, thread: Thread, instructions: object = "") -> CompactionOutcome:
        async with self._lock:
            try:
                session_id, _, _ = await self._ensure_acp_session(
                    thread,
                    worktree=thread.worktree.path,
                )
                extra = instructions.strip() if isinstance(instructions, str) else ""
                prompt_text = f"/compact {extra}" if extra else "/compact"
                state = AcpTurnStreamState()
                async for frame in self._process.prompt(
                    session_id,
                    [{"type": "text", "text": prompt_text}],
                ):
                    for _ in frame.apply(state, self._remember_applied):
                        pass
                return PlacedCompaction(
                    summary=state.full_text or "Compacted via Antigravity ACP",
                    tokens_before=UnreportedCount(),
                )
            except (AcpRpcError, ConnectionError, TimeoutError) as error:
                return FailedCompaction(str(error))


AntigravityAcpBackend.require_event_family(AntigravityAcpUpdate)
