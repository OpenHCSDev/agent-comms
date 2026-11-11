"""Open the selected native session before deciding whether input may start."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import aclosing
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms import agent_events as events
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.backend import NativeSession
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_startup import NativeStartup
from agent_comms.execution.participant_identity import ParticipantIdentity
from agent_comms.execution.prompt_state import PreparedSession
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.pi_payloads import StateData

if TYPE_CHECKING:
    from agent_comms.pi_native_backend import PersistentPiSession


class NativeSessionPreparation(NativeSession):
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
        owner: ParticipantIdentity,
        session_directory: Path,
        environment: dict[str, str],
        session_file: str,
        observe: Callable[[StateData, events.AgentInfo], Awaitable[None]],
    ) -> StateData:
        async with persistent.lock:
            return await cls.open_acquired(persistent, agent_bin, agent_args,
                package=package, worktree=worktree, root=root, owner=owner, session_directory=session_directory, environment=environment,
                session_file=session_file, observe=observe)

    @classmethod
    async def open_acquired(
        cls, persistent: PersistentPiSession, agent_bin: str, agent_args: Sequence[str], *,
        package: VerifiedNativePackage, worktree: str, root: Path, owner: ParticipantIdentity, session_directory: Path,
        environment: dict[str, str], session_file: str,
        observe: Callable[[StateData, events.AgentInfo], Awaitable[None]],
    ) -> StateData:
        """Keep the session's child owned through launch, observation and retention."""
        launch = await Coordination.run_worker(partial(
            persistent.child_state.managed_launch,
            agent_bin, tuple(agent_args), worktree=Path(worktree),
            package=package, root=root, owner=owner, directory=session_directory,
            environment=environment, session_file=session_file,
        ))
        preparation = cls(launch, native_session=persistent,
                          startup=NativeStartup.for_launch(launch))
        async with launch.session.files(root).accept_input():
            async with aclosing(preparation.run()) as stream:
                async for event in stream:
                    event.require_preparation_success()
            state = preparation.native.attestation.require_state()
            await observe(state, state.agent_info(preparation))
            return state
