"""Open the selected native session before deciding whether input may start."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path

from . import agent_events as events
from . import backend
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .pi_payloads import StateData
from .session_fence import session_writer_fence
from .turn_admission import UnwrittenPrompt


class PreparedSession(UnwrittenPrompt):
    acknowledged = False

    def permits_retention(self, session):
        return session.output.clean


class NativeSessionPreparation(backend.TurnSession):
    """Share native launch/attestation with turns; retain an idle child, send no prompt."""

    async def input_ready(self) -> None:
        state = self.native.attestation.state
        assert state is not None
        identity = self.native.attestation.identity
        if identity is None:
            raise NativePiUnavailable("Native preparation did not attest the saved session")
        self.launch.session.attest(identity)
        if (
            state.is_streaming is not False
            or state.is_compacting is not False
            or state.pending_message_count != 0
        ):
            raise NativePiUnavailable("Native preparation did not attest the idle saved session")
        self.active_session_file = identity.session_file
        self.admission = PreparedSession()
        self.finished = True

    async def finish_result(self) -> AsyncIterator[events.AgentEvent]:
        if not self.native_session.custody.retained:
            self.admission.raise_native_failure(
                NativePiUnavailable(
                    self.output.failure_text or "Native session preparation failed"
                ),
                self.native.attestation,
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
        return await cls.open_launch(persistent, launch)

    @classmethod
    async def open_launch(
        cls, persistent: backend.PersistentPiSession, launch: NativePiRpcLaunch
    ) -> StateData:
        """Attest the original selected launch without reserving or sending input."""
        preparation = cls(launch, "", persistent_session=persistent)
        owner = asyncio.current_task()
        async with session_writer_fence(launch.session.session_file), persistent.lock:
            try:
                async with aclosing(preparation.run()) as stream:
                    async for event in stream:
                        if isinstance(event, events.Done) and not event.ok:
                            raise NativePiUnavailable(event.text)
                state = preparation.native.attestation.state
                assert state is not None
                return state
            finally:
                if owner is not None:
                    await backend.terminate_task_process(owner)
