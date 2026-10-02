"""Turn progress, bounded waits and retry diagnostics owned by one watchdog."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
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


class ProgressWatchdog:
    """Own clocks and observed progress; never grant input or replay authority."""

    def __init__(
        self, model_wait_timeout: float | None, input_timeout: float, preflight_timeout: float
    ):
        self.model_wait_timeout = model_wait_timeout
        self.input_timeout = input_timeout
        self.preflight_timeout = preflight_timeout
        self.prompt_start_deadline: float | None = None
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
        self.preflight_wait_started_at = self.clock()
        self.preflight_budget = NATIVE_STARTUP_POLICY.readiness_timeout(
            self.session_bytes, base_seconds=self.preflight_timeout
        )
        self.preflight_deadline = self.preflight_wait_started_at + self.preflight_budget
        self.now = self.last_model_progress = self.clock()

    def await_input(self) -> None:
        self.prompt_start_deadline = self.clock() + self.input_timeout

    def compacted(self, input_started: bool) -> None:
        if not input_started and self.prompt_start_deadline is not None:
            self.prompt_start_deadline = self.now + self.input_timeout

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

    def read_timeout(self, session: TurnSession) -> float | None:
        if session.stats.requested:
            timeout: float | None = 5.0
        elif session.awaiting_native_attestation:
            timeout = max(0.0, self.preflight_deadline - self.clock())
        elif session.active_tools or self.model_wait_timeout is None:
            timeout = None
        else:
            timeout = max(0.0, self.last_model_progress + self.model_wait_timeout - self.clock())
        if (
            self.prompt_start_deadline is not None
            and not session.started_input
            and not self.phase.pauses_input_clock
        ):
            start_wait = max(0.0, self.prompt_start_deadline - self.clock())
            timeout = min(timeout, start_wait) if timeout is not None else start_wait
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
        try:
            done, _ = await asyncio.wait(
                tasks, timeout=self.read_timeout(session), return_when=asyncio.FIRST_COMPLETED
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
            started = self.preflight_wait_started_at
        elif (
            self.prompt_start_deadline is not None
            and not session.started_input
            and not self.phase.pauses_input_clock
        ):
            reason, phase = "input_start_timeout", "await_input"
            started = self.prompt_start_deadline - self.input_timeout
        else:
            stalled = self.phase.stalled(self.prompt_accepted)
            reason, phase = stalled.stall_reason, stalled.declared_name
            started = self.last_model_progress
        return TimeoutError(
            f"{reason}: phase={phase}; no progress for "
            f"{self.clock() - started:.3f}s; input_started={session.started_input}"
        )

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if session.awaiting_native_attestation:
            elapsed_ms = round((self.clock() - self.launch_started_at) * 1000)
            wait_ms = round((self.clock() - self.preflight_wait_started_at) * 1000)
            session.output.preflight_failure = FailureReason.PREFLIGHT_TIMEOUT
            session.output.diagnostic = {
                "elapsed_ms": elapsed_ms,
                "wait_ms": wait_ms,
                "spawn_ms": self.spawn_ms,
                "budget_ms": round(self.preflight_budget * 1000),
            }
            if self.session_bytes is not None:
                session.output.diagnostic["session_bytes"] = self.session_bytes
            session_size = self.session_bytes if self.session_bytes is not None else "unknown"
            session.output.record_failure(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight timed out "
                    f"(phase=await_get_state, elapsed_ms={elapsed_ms}, "
                    f"wait_ms={wait_ms}, budget_ms={round(self.preflight_budget * 1000)}, "
                    f"spawn_ms={self.spawn_ms}, session_bytes={session_size})."
                )
            )
            await session.native.proc.stop()
            session.finished = True
            return
        if (
            self.prompt_start_deadline is not None
            and (not session.started_input)
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
