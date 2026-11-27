"""Antigravity ACP runtime as a first-class ``AgentBackend``.

All Antigravity threads inside a worker share ONE multiplexed ``agy acp`` process
through ``AntigravityAcpPool``, giving every Antigravity thread native ``agent-comms``
semantics — ``new``, ``start``, ``stop``, ``fork`` (WAL-safe SQLite clone +
``trajectory_meta`` reassignment), ``configure`` (model + thinking level), ``compact``,
and ``abort`` — with zero per-thread TUI processes and zero tmux poking.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from agent_comms import agent_events as events
from agent_comms.agent_backend import (
    CompactionOutcome,
    FailedCompaction,
    Forks,
    PlacedCompaction,
    RuntimeLocation,
    ThreadServingBackend,
)
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.antigravity_acp import (
    AcpRpcError,
    AntigravityAcpPool,
    AntigravityAcpProcess,
    AntigravitySessionFiles,
    AntigravitySessionStorage,
)
from agent_comms.execution.antigravity_acp_events import (
    ANTIGRAVITY_ACP_EVENT_KINDS,
    AcpTurnStreamState,
    AntigravityAcpUpdate,
)
from agent_comms.execution.child_process import NoProcess, ProcessState
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.native_arguments import AntigravityProvider
from agent_comms.execution.runtime_observations import (
    AgentInfo,
    ReportedNativeSessionPath,
)
from agent_comms.execution.turn_failure import BackendFailed
from agent_comms.execution.turn_inbox import InputContent
from agent_comms.execution.unstarted_runtime import ExternalTranscriptLocation
from agent_comms.nominal.field_codec import AcpSessionId, InputId
from agent_comms.pi_counts import UnreportedCount
from agent_comms.registry.thread_model import DeclaredModel, ThreadModel, UndeclaredModel


@dataclass(frozen=True)
class AntigravitySession(ExternalTranscriptLocation, declared_name="antigravity_session"):
    """On-disk Antigravity conversation database (``conversations/<uuid>.db``)."""

    path: str

    @classmethod
    def fresh(cls, *, home: Path | None = None) -> Self:
        storage = AntigravitySessionStorage.for_id(str(uuid.uuid4()), home=home)
        return cls(str(storage.db_path))

    @property
    def storage(self) -> AntigravitySessionStorage:
        return AntigravitySessionStorage.from_db_path(Path(self.path))

    def last_written(self) -> float:
        return self.storage.last_written()

    def storage_files(self) -> tuple[AntigravitySessionFiles, ...]:
        return (AntigravitySessionFiles(self.storage),)

    @property
    def saved_model(self) -> ThreadModel:
        observed = self.storage.latest_reported_model()
        if not observed:
            return UndeclaredModel()
        if "/" in observed:
            return DeclaredModel(observed)
        return DeclaredModel(f"antigravity/{observed}")


def _prompt_blocks(content: InputContent) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    if content.text:
        blocks.append({"type": "text", "text": content.text})
    for image in content.images:
        blocks.append({
            "type": "image",
            "mimeType": image.mime_type,
            "data": base64.b64encode(image.data).decode("ascii"),
        })
    return blocks


def _selected_model_id(*, provider: object = None, model_id: str = "") -> str:
    del provider
    return model_id


class AntigravityAcpBackend(Forks, ThreadServingBackend, declared_name="antigravity_acp"):
    """Antigravity over ACP: many concurrent thread sessions multiplexed on one ``agy acp`` server."""

    location_type: ClassVar[type[RuntimeLocation]] = AntigravitySession
    event_kinds: ClassVar[frozenset[str]] = ANTIGRAVITY_ACP_EVENT_KINDS
    provider_type: ClassVar[type[AntigravityProvider]] = AntigravityProvider

    @classmethod
    async def apply_setting(cls, owner, configuration, thread, command) -> None:
        del configuration
        await owner.store_direct_setting(thread, command)

    def __init__(self, process: AntigravityAcpProcess) -> None:
        self._process = process
        self._acp_session_id: str | None = None
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
        parent,
        launcher: str,
        runtime: LaunchRuntime,
        root: Path,
    ) -> AntigravitySession:
        del launcher, runtime, root
        source = AntigravitySessionStorage.from_db_path(Path(parent.runtime.path))
        cloned = await asyncio.to_thread(source.clone_to, str(uuid.uuid4()))
        return AntigravitySession(str(cloned.db_path))

    @property
    def runtime_process(self) -> ProcessState:
        identity = self._process.process_identity
        return identity if identity is not None else NoProcess()

    def _remember_applied(self, state: AcpTurnStreamState) -> None:
        if state.current_model:
            self._applied_model = state.current_model
        if state.thought_level:
            self._applied_thinking = state.thought_level

    async def _ensure_acp_session(
        self,
        thread,
        *,
        worktree: Path,
    ) -> tuple[str, bool, Path]:
        storage = AntigravitySessionStorage.from_db_path(Path(thread.writable_runtime.path))
        if self._acp_session_id is not None and self._process.alive:
            return self._acp_session_id, False, storage.db_path
        if storage.exists:
            descriptor = await self._process.resume_session(storage.conversation_id, worktree)
            self._acp_session_id = descriptor.session_id
            self._applied_model = descriptor.current_model
            self._applied_thinking = descriptor.thought_level
            return descriptor.session_id, False, storage.db_path
        descriptor = await self._process.new_session(worktree)
        self._acp_session_id = descriptor.session_id
        self._applied_model = descriptor.current_model
        self._applied_thinking = descriptor.thought_level
        actual = AntigravitySessionStorage(root=storage.root, conversation_id=descriptor.session_id)
        return descriptor.session_id, True, actual.db_path

    async def _sync_configuration(self, session_id: str, thread) -> None:
        wanted_model = thread.model.set_model(_selected_model_id)
        if wanted_model and wanted_model != self._applied_model:
            await self._process.set_config_option(session_id, "model", wanted_model)
            self._applied_model = wanted_model
        wanted_thinking = thread.thinking_level.wire_value()
        if wanted_thinking and wanted_thinking != self._applied_thinking:
            await self._process.set_config_option(session_id, "thought_level", wanted_thinking)
            self._applied_thinking = wanted_thinking

    async def run_turn(
        self,
        thread,
        content: InputContent,
        delivery,
    ) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            native_input_id = InputId.mint()
            with delivery.send_boundary.send_original(native_input_id, content.text) as outcome:
                if not outcome.admitted:
                    yield events.Done(text=outcome.reason, ok=False)
                    return
                delivery.input_start.original(native_input_id, content.text)
            yield events.PromptAccepted()
            for started_event in delivery.input_start.started_events:
                yield started_event

            try:
                session_id, created, db_path = await self._ensure_acp_session(
                    thread, worktree=Path(delivery.worktree),
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
                async for frame in self._process.prompt(session_id, _prompt_blocks(content)):
                    for event in frame.apply(state, self._remember_applied):
                        yield event
                yield events.StreamSettled()
                yield events.Done(text=state.full_text, ok=True)
            except (AcpRpcError, ConnectionError, OSError) as error:
                message = str(error)
                yield events.Error(text=message)
                yield events.Done(text=message, ok=False, failure=BackendFailed(message))

    async def abort[Result](self, owner: asyncio.Task[Result]) -> None:
        del owner
        if self._acp_session_id is not None:
            with suppress(AcpRpcError, ConnectionError, OSError):
                await self._process.cancel(self._acp_session_id)

    async def close_idle(self) -> None:
        async with self._lock:
            session_id = self._acp_session_id
            self._acp_session_id = None
            self._applied_model = ""
            self._applied_thinking = ""
            if session_id is not None:
                await self._process.close_session(session_id)

    async def compact(self, thread, instructions: str | None) -> CompactionOutcome:
        async with self._lock:
            try:
                session_id, _, _ = await self._ensure_acp_session(
                    thread, worktree=thread.worktree.path,
                )
                prompt_text = f"/compact {instructions.strip()}" if instructions else "/compact"
                state = AcpTurnStreamState()
                async for frame in self._process.prompt(
                    session_id, [{"type": "text", "text": prompt_text}],
                ):
                    for _ in frame.apply(state, self._remember_applied):
                        pass
                return PlacedCompaction(
                    summary=state.full_text or "Compacted via Antigravity ACP",
                    tokens_before=UnreportedCount(),
                )
            except (AcpRpcError, ConnectionError, OSError) as error:
                return FailedCompaction(str(error))


AntigravityAcpBackend.require_event_family(AntigravityAcpUpdate)
