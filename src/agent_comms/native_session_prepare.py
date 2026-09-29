"""Open the selected native session before deciding whether input may start."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path

from . import agent_events as events
from . import backend
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .native_startup import NativeStartupAdmission
from .pi_payloads import StateData
from .session_fence import session_writer_fence


class NativeSessionPreparation(backend.TurnSession):
    """Share native launch/attestation with turns; retain an idle child, send no prompt."""

    async def input_ready(self) -> None:
        state = self.state
        if (
            state.session_file != self.session_file
            or not state.session_id
            or state.is_streaming is not False
            or state.is_compacting is not False
            or state.pending_message_count != 0
        ):
            raise NativePiUnavailable("Native preparation did not attest the idle saved session")
        self.active_session_file = state.session_file
        self.initial_session_file = state.session_file
        self.initial_session_id = state.session_id
        self.finished = True

    def can_retain(self) -> bool:
        return bool(
            self.native_capability_confirmed
            and not self.output.failure_text
            and not self.session_identity_uncertain
            and self.proc.returncode is None
            and self.initial_session_id
            and self.active_session_file == self.session_file
            and self.revision is not None
        )

    async def finish_result(self) -> AsyncIterator[events.AgentEvent]:
        if not self.retained:
            raise NativePiUnavailable(
                self.output.failure_text or "Native session preparation failed"
            )
        if False:
            yield

    @classmethod
    async def open(
        cls,
        persistent: backend.PersistentPiSession,
        agent_bin: str,
        agent_args: Sequence[str],
        *,
        worktree: str,
        environment: dict[str, str],
        session_file: str,
    ) -> StateData:
        launch = await asyncio.to_thread(
            NativePiRpcLaunch.managed,
            agent_bin,
            tuple(agent_args),
            worktree=Path(worktree),
            environment=environment,
            session_file=session_file,
        )
        startup = NativeStartupAdmission(Path(environment["AGENT_COMMS_ROOT"]))
        preparation = cls(
            launch, "", session_file=session_file, persistent_session=persistent, startup=startup
        )
        owner = asyncio.current_task()
        async with session_writer_fence(session_file), persistent.lock:
            try:
                async with aclosing(preparation.run()) as stream:
                    async for event in stream:
                        if isinstance(event, events.Done) and not event.ok:
                            raise NativePiUnavailable(event.text)
                return preparation.state
            finally:
                startup.release()
                if owner is not None:
                    await backend.terminate_task_process(owner)
