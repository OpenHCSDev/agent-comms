"""Turn progress, bounded waits and retry diagnostics owned by one watchdog."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import agent_events as events
from . import turn_failure as failures
from . import turn_phase as phases
from .diagnostics import FailureReason
from .native_startup import NATIVE_STARTUP_POLICY

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import PiEvent
    from .pi_events import RetryAttemptEvent


@dataclass
class OwnedWait:
    """A budget charged only while its owner is actually awaiting progress."""

    budget: float
    remaining: float = field(init=False)
    elapsed: float = field(init=False, default=0.0)
    _started_at: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.remaining = self.budget

    @contextmanager
    def charge(self, clock: Callable[[], float]):
        if self._started_at is not None:
            raise RuntimeError("wait budget is already active")
        self._started_at = clock()
        try:
            yield self.remaining
        finally:
            assert self._started_at is not None
            charged = min(self.remaining, max(0.0, clock() - self._started_at))
            self.remaining -= charged
            self.elapsed += charged
            self._started_at = None

    def restart(self) -> None:
        if self._started_at is not None:
            raise RuntimeError("active wait budget cannot restart")
        self.remaining = self.budget
        self.elapsed = 0.0


class ProgressWatchdog:
    """Own clocks and observed progress; never grant input or replay authority."""

    def __init__(
        self, model_wait_timeout: float | None, input_timeout: float, preflight_timeout: float
    ):
        self.model_wait_timeout = model_wait_timeout
        self.input_timeout = input_timeout
        self.preflight_timeout = preflight_timeout
        self.input_wait: OwnedWait | None = None
        self.prompt_accepted = False
        self.phase: phases.TurnPhase = phases.PromptAcceptancePhase()
        self.retry_recovery_pending = False
        self.retry_recovery_reason = "provider_auto_retry_progress"

    def launching(self, clock: Callable[[], float], session_file: str | None) -> None:
        self.clock = clock
        self.launch_started_at = clock()
        self.session_bytes: int | None = None
        if session_file:
            with suppress(OSError):
                self.session_bytes = Path(session_file).stat().st_size

    def spawned(self, reused: bool) -> None:
        self.spawn_ms = 0 if reused else round((self.clock() - self.launch_started_at) * 1000)

    def reading(self) -> None:
        self.preflight_wait = OwnedWait(NATIVE_STARTUP_POLICY.readiness_timeout(
            self.session_bytes, base_seconds=self.preflight_timeout
        ))
        self.now = self.last_model_progress = self.clock()

    def await_input(self) -> None:
        self.input_wait = OwnedWait(self.input_timeout)

    def compacted(self, input_started: bool) -> None:
        if not input_started and self.input_wait is not None:
            self.input_wait.restart()

    def tick(self) -> None:
        self.now = self.clock()

    def progress(self) -> None:
        self.last_model_progress = self.now

    async def observe(
        self, event: PiEvent, session: TurnSession
    ) -> AsyncIterator[events.AgentEvent]:
        self.tick()
        if self.retry_recovery_pending and event.retry_progress:
            self.retry_recovery_pending = False
            yield self.state(
                "recovered", self.retry_recovery_reason, 0, phase=phases.ModelWaitPhase()
            )
        if event.accepts_prompt:
            self.prompt_accepted = True
            if event.invalidates_stop:
                session.output.final_assistant_stop = False
            self.progress()
            if not session.active_tools:
                self.phase = self.phase.model_progress()

    def transition(self, event: PiEvent, active_tools: set[str]) -> None:
        self.phase = event.observed_phase(self.phase.on(event, active_tools))

    def owned_wait(self, session: TurnSession) -> OwnedWait | None:
        if session.awaiting_native_attestation:
            return self.preflight_wait
        if (
            self.input_wait is not None
            and session.admission.awaiting_start
            and not self.phase.pauses_input_clock
        ):
            return self.input_wait
        return None

    def read_timeout(
        self, session: TurnSession, owned_timeout: float | None = None
    ) -> float | None:
        if session.awaiting_native_attestation:
            timeout: float | None = (
                self.preflight_wait.remaining if owned_timeout is None else owned_timeout
            )
        elif session.stats.requested:
            timeout = 5.0
        elif session.admission.awaiting_start:
            # Prompt acceptance is not model work. Until Pi publishes the
            # matching user start, the dedicated input-start budget owns the
            # wait and a short model lease must not race the pipe reader.
            assert owned_timeout is not None
            timeout = owned_timeout
        elif session.active_tools or (wait := self.phase.model_wait(self.model_wait_timeout)) is None:
            timeout = None
        else:
            timeout = max(0.0, self.last_model_progress + wait - self.clock())
        return timeout

    def state(
        self,
        state: str,
        reason_code: str,
        elapsed_ms: int,
        *,
        phase: phases.TurnPhase,
        attempt: RetryAttemptEvent | None = None,
    ) -> events.TurnState:
        return events.TurnState(
            state=state,
            reason_code=reason_code,
            elapsed_ms=max(0, elapsed_ms),
            phase=phase,
            attempt=attempt,
        )

    async def read(self, session: TurnSession) -> bytes:
        if session.rejected_signal.is_set():
            return b"\n"
        read_task = asyncio.create_task(session.native.reader.readline())
        reject_task = asyncio.create_task(session.rejected_signal.wait())
        finish_task = (
            asyncio.create_task(session.finish_event.wait())
            if session.finish_event is not None and (not session.stats.requested)
            else None
        )
        tasks = {read_task, reject_task}
        if finish_task is not None:
            tasks.add(finish_task)
        owned_wait = self.owned_wait(session)
        try:
            with (
                owned_wait.charge(self.clock)
                if owned_wait is not None
                else nullcontext()
            ) as owned_timeout:
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=self.read_timeout(session, owned_timeout),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            if not done:
                # A busy owner loop can resume its deadline before the pipe
                # reader's already-ready continuation. Settle queued callbacks
                # before cancelling that read; this grants no additional wait
                # budget and leaves attestation with its existing owner.
                await asyncio.sleep(0)
                done = {task for task in tasks if task.done()}
            if not done:
                raise self.expired_wait(session)
            if read_task in done:
                return read_task.result()
            if finish_task is not None and finish_task in done:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                if session.awaiting_native_attestation:
                    return b""
                await session.stats.request(session)
            return b"\n"
        finally:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def expired_wait(self, session: TurnSession) -> TimeoutError:
        """Describe the expired operation using the existing lifecycle clocks."""
        if session.awaiting_native_attestation:
            reason, phase = "native_preflight_timeout", "await_get_state"
            elapsed = self.preflight_wait.elapsed
        elif (
            self.input_wait is not None
            and session.admission.awaiting_start
            and not self.phase.pauses_input_clock
        ):
            reason, phase = "input_start_timeout", "await_input"
            elapsed = self.input_wait.elapsed
        else:
            stalled = self.phase.stalled(self.prompt_accepted)
            reason, phase = stalled.stall_reason, stalled.declared_name
            elapsed = self.clock() - self.last_model_progress
        return TimeoutError(
            f"{reason}: phase={phase}; no progress for "
            f"{elapsed:.3f}s; input_started={session.started_input}"
        )

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if session.awaiting_native_attestation:
            elapsed_ms = round((self.clock() - self.launch_started_at) * 1000)
            wait_ms = round(self.preflight_wait.elapsed * 1000)
            session.output.preflight_failure = FailureReason.PREFLIGHT_TIMEOUT
            session.output.diagnostic = {
                "elapsed_ms": elapsed_ms,
                "wait_ms": wait_ms,
                "spawn_ms": self.spawn_ms,
                "budget_ms": round(self.preflight_wait.budget * 1000),
            }
            if self.session_bytes is not None:
                session.output.diagnostic["session_bytes"] = self.session_bytes
            session_size = self.session_bytes if self.session_bytes is not None else "unknown"
            session.output.record_failure(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight timed out "
                    f"(phase=await_get_state, elapsed_ms={elapsed_ms}, "
                    f"wait_ms={wait_ms}, budget_ms={round(self.preflight_wait.budget * 1000)}, "
                    f"spawn_ms={self.spawn_ms}, session_bytes={session_size})."
                )
            )
            await session.native.proc.stop()
            session.finished = True
            return
        if (
            self.input_wait is not None
            and session.admission.awaiting_start
            and (not self.phase.pauses_input_clock)
        ):
            session.output.record_failure(
                failures.InputMissing("Pi RPC run ended without this prompt's user message start.")
            )
            await session.native.proc.stop()
            session.finished = True
            return
        if session.stats.requested:
            session.finished = True
            return
        elapsed_ms = round((self.clock() - self.last_model_progress) * 1000)
        stalled_phase = self.phase.stalled(self.prompt_accepted)
        reason_code = stalled_phase.stall_reason
        if self.prompt_accepted:
            yield self.state(
                "model_stalled", reason_code, elapsed_ms, phase=stalled_phase
            )
        yield self.state("aborting", reason_code, elapsed_ms, phase=phases.ShutdownPhase())
        await session.abort_stalled_rpc()
        for input_id in session.started_during_abort:
            yield events.InputStarted(id=input_id)
        failed_elapsed_ms = round((self.clock() - self.last_model_progress) * 1000)
        yield self.state("failed", reason_code, failed_elapsed_ms, phase=phases.ShutdownPhase())
        session.output.record_failure(
            failures.ModelStalled(
                f"Model produced no RPC progress for {self.model_wait_timeout:g} seconds."
                if self.prompt_accepted
                else f"Pi did not accept the prompt within {self.model_wait_timeout:g} seconds."
            )
        )
        session.finished = True
        return
