"""Declaration-owned turn waits, progress budgets and retry diagnostics."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar

from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from . import agent_events as events
from . import turn_failure as failures
from . import turn_phase as phases
from agent_comms.nominal.declared_family import DeclaredFamily
from .native_startup import SessionLoad

if TYPE_CHECKING:
    from .backend import TurnSession
    from .native_startup import NativeStartupPolicy
    from .pi_events import PiEvent


class TurnWaitExpired(TimeoutError):
    """The selected wait ran out; it alone owns the expiry that follows."""

    def __init__(self, wait: TurnWait, message: str):
        super().__init__(message)
        self.wait = wait


class TurnWait(DeclaredFamily, affix="Wait"):
    """Own the wait's budget, charging, diagnostic and terminal behaviour."""

    reason: ClassVar[str]
    phase: ClassVar[str]

    def __init__(self, budget: float = 0.0):
        self.budget = budget
        self.elapsed = 0.0

    def fresh(self) -> TurnWait:
        """Each turn charges its own clock, even when given the same wait policy."""
        return type(self)(self.budget)

    def restart(self) -> None:
        self.elapsed = 0.0

    def timeout(self) -> float | None:
        return max(0.0, self.budget - self.elapsed)

    @contextmanager
    def charge(self, clock: Callable[[], float]) -> Iterator[None]:
        started_at = clock()
        remaining = max(0.0, self.budget - self.elapsed)
        try:
            yield
        finally:
            self.elapsed += min(remaining, max(0.0, clock() - started_at))

    def diagnosis(self, session: TurnSession) -> str:
        return f"{self.reason}: phase={self.phase}"

    def expired(self, session: TurnSession) -> TurnWaitExpired:
        return TurnWaitExpired(
            self,
            f"{self.diagnosis(session)}; no progress for "
            f"{self.elapsed:.3f}s; input_started={session.started_input}"
        )

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.finish_request()
        return
        yield  # an expiry without events is still this event stream


class PreflightWait(TurnWait):
    reason = failures.NativePreflightTimeout.declared_name
    phase = "await_get_state"

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        watchdog = session.watchdog
        elapsed_ms = round((watchdog.clock() - watchdog.launch_started_at) * 1000)
        wait_ms = round(self.elapsed * 1000)
        budget_ms = round(self.budget * 1000)
        session.output.diagnostic = {
            "elapsed_ms": elapsed_ms,
            "wait_ms": wait_ms,
            "spawn_ms": watchdog.spawn_ms,
            "budget_ms": budget_ms,
        }
        session.output.diagnostic.update(watchdog.session_load.diagnostic)
        session_size = watchdog.session_load.summary
        session.output.record_failure(
            failures.NativePreflightTimeout(
                "Pi native input-ID capability preflight timed out "
                f"(phase={self.phase}, elapsed_ms={elapsed_ms}, "
                f"wait_ms={wait_ms}, budget_ms={budget_ms}, "
                f"spawn_ms={watchdog.spawn_ms}, session_bytes={session_size})."
            )
        )
        await session.native.shutdown()
        async for event in super().expire(session):
            yield event


class InputStartWait(TurnWait):
    reason = "input_start_timeout"
    phase = "await_input"

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.output.record_failure(
            failures.InputMissing("Pi RPC run ended without this prompt's user message start.")
        )
        await session.native.shutdown()
        async for event in super().expire(session):
            yield event


class ModelWait(TurnWait):
    def __init__(self, budget: float = 360.0):
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("A model progress wait must be positive and finite")
        super().__init__(budget)

    def diagnosis(self, session: TurnSession) -> str:
        stalled = session.watchdog.phase.stalled()
        return f"{stalled.stall_reason}: phase={stalled.declared_name}"

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        await session.abort_stalled_rpc()
        for started in session.started_during_abort:
            yield started
        session.output.record_failure(failures.ModelStalled(
            session.watchdog.phase.stalled().stall_message(self.budget)
        ))
        async for event in super().expire(session):
            yield event


DEFAULT_MODEL_WAIT = ModelWait()


class UnchargedWait:
    """Polling and unbounded operations do not consume a progress budget."""

    @contextmanager
    def charge(self, clock: Callable[[], float]) -> Iterator[None]:
        yield


class PollWait(UnchargedWait, TurnWait):
    reason = "session_stats_timeout"
    phase = "await_stats"

    def timeout(self) -> float:
        return 5.0

    async def expire(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.stats.expired(session)
        async for event in super().expire(session):
            yield event


class UnboundedWait(UnchargedWait, TurnWait):
    reason = "unbounded_wait_interrupted"
    phase = "unbounded"

    def timeout(self) -> None:
        return None


class ProgressWatchdog(MroDispatch):
    """Select a declared wait once; never grant input or replay authority."""

    def __init__(
        self, model_wait: TurnWait, input_timeout: float, preflight_timeout: float,
        startup: NativeStartupPolicy,
    ):
        self.preflight_wait = PreflightWait(preflight_timeout)
        self.startup = startup
        self.input_wait = InputStartWait(input_timeout)
        self.model_wait = model_wait.fresh()
        self.poll_wait = PollWait()
        self.unbounded_wait = UnboundedWait()
        self.phase: phases.TurnPhase = phases.PromptAcceptancePhase()

    def launching(self, clock: Callable[[], float], session_file: str | None) -> None:
        self.clock = clock
        self.launch_started_at = clock()
        self.session_load = SessionLoad.capture(session_file)

    def spawned(self, reused: bool) -> None:
        self.spawn_ms = 0 if reused else round((self.clock() - self.launch_started_at) * 1000)

    def reading(self) -> None:
        self.preflight_wait.budget = self.startup.readiness_timeout(
            self.session_load, base_seconds=self.preflight_wait.budget
        )
        self.preflight_wait.restart()

    def await_input(self) -> None:
        self.input_wait.restart()

    def compacted(self, input_started: bool) -> None:
        if not input_started:
            self.input_wait.restart()

    def progress(self) -> None:
        # Both budgets measure native silence: an input queued behind a run
        # that keeps producing events is waiting its turn, not lost.
        self.model_wait.restart()
        self.input_wait.restart()

    def accepted(self) -> None:
        """The declared phase is the acceptance fact, not a parallel boolean."""
        self.progress()
        self.phase = self.phase.model_progress()

    @handles(phases.AcceptsPrompt)
    def accept_prompt(self, event: PiEvent, session: TurnSession) -> None:
        if event.invalidates_stop:
            session.output.final_assistant_stop = False
        self.accepted()

    def transition(self, event: PiEvent) -> None:
        self.phase = event.on(self.phase)

    def wait_for(self, session: TurnSession) -> TurnWait:
        if session.awaiting_native_attestation:
            return self.preflight_wait
        active = self.phase.model_wait(self.model_wait, self.unbounded_wait)
        if session.prompt_state.awaiting_start and not self.phase.pauses_input_clock:
            active = self.input_wait
        return session.stats.wait_for(self, active)

    async def read(self, session: TurnSession) -> bytes:
        if session.rejected_signal.is_set():
            return b"\n"
        read_task = asyncio.create_task(session.native.reader.readline())
        reject_task = asyncio.create_task(session.rejected_signal.wait())
        finish_tasks = session.stats.finish_waiters(session.finish_signal)
        tasks = {read_task, reject_task, *finish_tasks}
        forwarding = session.native.forwarding
        try:
            while True:
                observed = tasks.union(forwarding.tasks, session.stats.tasks)
                wait = self.wait_for(session)
                with wait.charge(self.clock):
                    done, _ = await asyncio.wait(
                        observed, timeout=wait.timeout(), return_when=asyncio.FIRST_COMPLETED
                    )
                if not done:
                    # Retire an already-ready pipe continuation, never extend a provider wait.
                    await asyncio.sleep(0)
                    done = {task for task in observed if task.done()}
                if not done:
                    raise wait.expired(session)
                if await forwarding.finish_if_done(session.native, done):
                    return b""
                advanced = session.stats.advance_if_done(session, done)
                if read_task in done:
                    return read_task.result()
                if advanced:
                    # Keep this exact read alive while the snapshot starts;
                    # an internal task completion is not a fabricated RPC line.
                    continue
                if done.intersection(finish_tasks):
                    session.finish_signal.consumed()
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    if session.awaiting_native_attestation:
                        return b""
                    await session.native.stop_forwarding()
                    await session.stats.request(session)
                return b"\n"
        finally:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
