"""Open the selected native session before deciding whether input may start."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import aclosing
from pathlib import Path
from functools import partial
from typing import TYPE_CHECKING

from . import agent_events as events
from . import backend
from .native_pi import NativePiUnavailable
from .native_package import VerifiedNativePackage
from .native_startup import NativeStartup
from .coordinator import Coordination
from .pi_payloads import StateData
from .session_fence import session_writer_fence
from .prompt_state import UnwrittenPrompt

if TYPE_CHECKING:
    from .pi_native_backend import PersistentPiSession
    from .following_inputs import FollowingInputs


class PreparedSession(UnwrittenPrompt):
    acknowledged = False

    def permits_retention(self, session: backend.TurnSession) -> bool:
        return session.output.clean

    async def deliver_to(
        self, inputs: FollowingInputs, session: backend.TurnSession,
    ) -> bool:
        session.native.forget_context()
        state = session.native.attestation.state
        assert state is not None
        identity = session.native.attestation.identity
        if identity is None:
            raise NativePiUnavailable("Native preparation did not attest the saved session")
        session.launch.session.attest(identity)
        if (
            state.is_streaming is not False
            or state.is_compacting is not False
            or not state.pending_message_count.matches(0)
        ):
            raise NativePiUnavailable("Native preparation did not attest the idle saved session")
        session.active_session_file = identity.session_file
        await session.stats.request(session)
        return True


class NativeSessionPreparation(backend.TurnSession):
    """Share native launch/attestation with turns; retain an idle child, send no prompt."""
    prompt_type = PreparedSession

    def finish_result(self) -> Iterable[events.AgentEvent]:
        if not self.native_session.child_state.retained:
            self.prompt_state.raise_native_failure(
                NativePiUnavailable(
                    self.output.failure_text or "Native session preparation failed"
                ),
                self.native.attestation,
            )
        return ()

    @classmethod
    async def open(
        cls,
        persistent: PersistentPiSession,
        agent_bin: str,
        agent_args: Sequence[str],
        *,
        package: VerifiedNativePackage,
        worktree: str,
        root: Path,
        environment: dict[str, str],
        session_file: str,
        observe: Callable[[StateData, events.AgentInfo], Awaitable[None]],
    ) -> StateData:
        async with persistent.lock:
            return await cls.open_acquired(persistent, agent_bin, agent_args,
                package=package, worktree=worktree, root=root, environment=environment,
                session_file=session_file, observe=observe)

    @classmethod
    async def open_acquired(
        cls, persistent: PersistentPiSession, agent_bin: str, agent_args: Sequence[str], *,
        package: VerifiedNativePackage, worktree: str, root: Path, environment: dict[str, str], session_file: str,
        observe: Callable[[StateData, events.AgentInfo], Awaitable[None]],
    ) -> StateData:
        """Keep the session's child owned through launch, observation and retention."""
        owner = asyncio.current_task()
        launch = await Coordination.run_worker(partial(
            persistent.child_state.managed_launch,
            agent_bin, tuple(agent_args), worktree=Path(worktree),
            package=package, root=root,
            environment=environment, session_file=session_file,
        ))
        preparation = cls(launch, "", persistent_session=persistent, startup=NativeStartup.for_launch(launch))
        async with session_writer_fence(launch.session.session_file):
            try:
                async with aclosing(preparation.run()) as stream:
                    async for event in stream:
                        if isinstance(event, events.Done) and not event.ok:
                            raise NativePiUnavailable(event.text)
                state = preparation.native.attestation.state
                assert state is not None
                await observe(state, state.agent_info(preparation))
                return state
            finally:
                if owner is not None:
                    await backend.terminate_task_process(owner)
