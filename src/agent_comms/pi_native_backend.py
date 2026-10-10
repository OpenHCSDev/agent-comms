"""The Pi 0.85.1 runtime as an AgentBackend: one retained Pi RPC child per thread session.

``PiNativeBackend`` is the only entry point Core uses for this runtime. It owns
the per-session child (``PersistentPiSession``), its launch command, and the
conversions from Pi's RPC, session file and settings vocabulary to Core's
records. The transport and turn machinery stay in their modules (``backend``,
``native_custody``, ``pi_commands``, ``pi_events``, ``native_entries``) as this
backend's private implementation and are imported lazily: this module is
declared while ``threads`` is still importing.
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
    Compacts,
    CompactionOutcome,
    ConfiguresModel,
    ContextUsage,
    Disposition,
    EntryPage,
    EntryRef,
    FailedCompaction,
    Forks,
    InputContent,
    InterruptedWork,
    Interrupts,
    NothingToCompaction,
    PlacedCompaction,
    ReadsHistory,
    RecoversAfterCrash,
    ReportsContext,
    RuntimeLocation,
    TranscriptEntry,
    TurnDelivery,
)

if TYPE_CHECKING:
    from .agent_events import AgentEvent, AgentInfo
    from .comms import Comms
    from .native_arguments import NativeArguments
    from .native_custody import NativeCustody, PiSessionChild
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
    "summarization_retry_scheduled", "summarization_retry_attempt_start",
    "summarization_retry_finished",
    "steering_interrupt_started", "steering_interrupt_completed",
    "response", "extension_ui_request", "extension_error",
})


@dataclass(frozen=True)
class PiSessionFile(RuntimeLocation):
    """A Pi session JSONL file; derived from ``Thread.session_file``, never stored apart."""

    path: str

    @classmethod
    def of(cls, thread: Thread) -> Self:
        return cls(thread.require_saved_session())


class PersistentPiSession:
    """Serialize borrowing and retirement of one native session's actual child."""

    def __init__(self) -> None:
        from .native_custody import EmptyNative

        self.lock = asyncio.Lock()
        self.custody: NativeCustody = EmptyNative()

    @property
    def available(self) -> bool:
        return self.custody.available

    async def open(self, launch, *, reuse, startup, watchdog) -> PiSessionChild:
        from .native_custody import BorrowedNative, EmptyNative, PiSessionChild

        key = (launch, launch.configuration.auth_revision())
        child = self.custody.reuse(key) if reuse else None
        reused = child is not None
        if child is None:
            await self.close()
            attestation = launch.session.attestation()
            watchdog.launching(asyncio.get_running_loop().time, launch.session.session_file)
            with startup.measurements.operation("native_spawn"):
                child = await PiSessionChild.start(key, attestation)
            self.custody = BorrowedNative(child, self.custody)
        else:
            watchdog.launching(asyncio.get_running_loop().time, launch.session.session_file)
            self.custody = BorrowedNative(child, EmptyNative())
        watchdog.spawned(reused)
        return child

    def retain(self, child: PiSessionChild, identity: NativeSessionIdentity) -> bool:
        from .native_custody import RetainedNative
        from .session_revision import SessionRevision, SessionRevisionUnavailable

        if not child.proc.alive():
            return False
        try:
            revision = SessionRevision.observe(identity.session_file).require_available()
        except SessionRevisionUnavailable:
            return False
        self.custody = RetainedNative(child, identity, revision)
        return True

    async def close(self) -> None:
        await self._finish_retirement(self.custody.retire())

    async def _finish_retirement(self, retiring: NativeCustody) -> None:
        from .native_custody import NativeCleanupFailed

        self.custody = retiring
        try:
            self.custody = await self.custody.closed()
        except NativeCleanupFailed as error:
            self.custody = error.successor
            raise

    async def close_idle(self) -> None:
        async with self.lock:
            await self.close()


class PiNativeBackend(
    PersistentPiSession,
    AgentBackend,
    Interrupts,
    Compacts,
    ReportsContext,
    ReadsHistory,
    Forks,
    RecoversAfterCrash,
    ConfiguresModel,
):
    """Pi 0.85.1 over RPC: one retained child per thread session in this worker.

    The instance is the session's child custody. Its launch command (``agent_bin``
    plus ``agent_args``) and the publisher of attested session info are fixed
    when the worker creates it.
    """

    location_type: ClassVar[type[RuntimeLocation]] = PiSessionFile
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

    def arguments(self, thread: Thread) -> tuple[str, ...]:
        """Pi's command-line arguments for this thread's model and thinking level."""
        from .pi_vocabulary import ThinkingLevel

        return self.agent_args.with_model(thread.model).with_thinking(
            ThinkingLevel.optional_name(thread.thinking_level)
        ).argv

    # -- lifecycle --------------------------------------------------------

    async def prepare(self, thread: Thread, *, open_native=None) -> StateData:
        """Launch (or reuse) and attest the idle child for ``thread``'s saved session."""
        from .coordinator import Coordination
        from .native_session_prepare import NativeSessionPreparation

        if thread.session_file is None:
            raise ValueError("Native preparation requires a saved session")
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
            environment=environment,
            session_file=thread.session_file,
            observe=partial(self.observe, thread),
        )

    async def inspect(self, thread: Thread, request):
        """Answer one Pi query on the idle child, preparing it first when needed."""
        return await self.custody.inspect(self, partial(self.prepare, thread), request)

    # -- RunsTurns / Steers / Interrupts ------------------------------------

    async def run_turn(
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
            session_file=thread.session_file,
            steering_queue=delivery.inbox,
            finish_event=delivery.finish_event,
            send_boundary=delivery.send_boundary,
            interrupt_boundary=delivery.interrupt_boundary,
            native_start=delivery.input_started,
            request_observer=delivery.request_progress,
            persistent_session=self,
            ui_request=delivery.ui_request,
        ):
            yield event

    async def abort(self, owner: asyncio.Task[Any]) -> None:
        from . import backend

        await backend.terminate_task_process(owner)

    # -- ReportsContext ---------------------------------------------------

    async def context_usage(self, thread: Thread) -> ContextUsage:
        from .pi_commands import GetSessionStats

        stats = await self.inspect(thread, GetSessionStats())
        usage = stats.context_usage
        if usage is None:
            return ContextUsage(tokens=None, window=None)
        return ContextUsage(tokens=usage.tokens, window=usage.context_window)

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

    # -- ReadsHistory -----------------------------------------------------

    async def entries(self, thread: Thread, before: EntryRef | None, limit: int) -> EntryPage:
        """Decoded session-file entries, newest first; incomplete tails are skipped."""
        from .coordinator import Coordination

        return await Coordination.run_worker(
            partial(self.read_entries, Path(PiSessionFile.of(thread).path), before, limit)
        )

    @staticmethod
    def read_entries(path: Path, before: EntryRef | None, limit: int) -> EntryPage:
        from .native_transcript import NativeTranscript

        if limit <= 0:
            raise ValueError("A history page needs a positive limit")
        start = path.stat().st_size if before is None else before.order
        page: list[TranscriptEntry] = []
        for record in NativeTranscript(path).reverse(start):
            if record.entry is None:
                continue
            entry = record.entry
            page.append(TranscriptEntry(
                ref=EntryRef(record.start, entry.source_coordinates.id or ""),
                kind=entry.transcript_kind(),
                text=entry.transcript_text(),
                at=entry.recorded_at,
            ))
            if len(page) == limit:
                break
        older = page[-1].ref if len(page) == limit and page[-1].ref.order > 0 else None
        return EntryPage(tuple(page), older)

    # -- Forks ------------------------------------------------------------

    @classmethod
    async def fork(cls, parent: Thread, launcher: str) -> PiSessionFile:
        """Copy the parent's whole saved history with Pi's own session manager."""
        from .coordinator import Coordination
        from .native_fork import fork_native_session

        identity = await Coordination.run_worker(partial(
            fork_native_session, parent.require_saved_session(), parent.worktree, launcher,
        ))
        return PiSessionFile(identity.session_file)

    # -- RecoversAfterCrash -----------------------------------------------

    async def interrupted(self, thread: Thread) -> tuple[InterruptedWork, ...]:
        """Pi 0.85.1 keeps no resumable work: a dead child's request is gone, never resent."""
        return ()

    async def settle(self, thread: Thread, work: InterruptedWork, disposition: Disposition) -> None:
        raise ValueError("Pi 0.85.1 has no interrupted work to settle")

    async def resume(self, thread: Thread) -> None:
        """Nothing is scheduled by Pi itself after a restart; Core sends every input."""

    # -- ConfiguresModel --------------------------------------------------

    async def models(self, thread: Thread) -> tuple[str, ...]:
        from .pi_commands import GetAvailableModels

        data = await GetAvailableModels().discover(self.agent_bin, self.agent_args.argv)
        return tuple(dict.fromkeys(
            model.display_name for model in data.models if model.provider and model.id
        ))

    async def configure(self, thread: Thread, model: str | None, thinking: str | None) -> tuple[str, str]:
        from .native_pi import NativePiUnavailable
        from .pi_commands import SetModel, SetThinkingLevel
        from .pi_vocabulary import ThinkingLevel

        commands = []
        if model is not None:
            provider, model_id = model.split("/", 1)
            commands.append(SetModel(provider=provider, model_id=model_id))
        if thinking is not None:
            commands.append(SetThinkingLevel(level=thinking))
        if not commands:
            raise ValueError("A configuration change names a model or a thinking level")
        arguments = self.arguments(thread)
        for command in commands:
            state = await command.configure(self.agent_bin, arguments, worktree=Path(thread.worktree))
            arguments = self.agent_args.with_model(state.model.display_name).with_thinking(
                ThinkingLevel.optional_name(state.thinking_level)
            ).argv
        selected, level = state.model.display_name, ThinkingLevel.optional_name(state.thinking_level)
        if selected is None or level is None:
            raise NativePiUnavailable("Native configuration did not report model and thinking level")
        return selected, level
