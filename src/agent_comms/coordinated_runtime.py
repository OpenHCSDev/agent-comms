"""One captured batch, one exact lease, no automatic native input replay.

Preparation owns registry/participant/source identity; session enrollment owns
first-start authority; each reserved stage owns its settlement. The runner only
orders those lifetimes and cannot represent a half-initialized native attempt.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from metaclass_registry.mro_dispatch import MroDispatch

from agent_comms.comms import wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    RetryScheduled,
)
from agent_comms.coordination.snapshot import RecoverySnapshot
from agent_comms.execution.native_execution import NativeExecution, PackagedNativeExecution
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_pi import NativePiRpcLaunch
from agent_comms.execution.native_session_files import NativeSessionFiles
from agent_comms.execution.persistent_pi_session import PersistentPiSession
from agent_comms.execution.selected.actions import (
    CodingSelectedAction,
    NoSelectedWritePlans,
    SelectedAction,
    SelectedWriteAuthority,
)
from agent_comms.execution.selected.participant import NoLivePublication, SelectedParticipant
from agent_comms.execution.selected.result import CoordinatedTurn
from agent_comms.execution.selected.session import SelectedSession
from agent_comms.execution.selected.turn import SelectedAttempt, SelectedConsideration
from agent_comms.execution.turn_failure import RetryableProviderFailure
from agent_comms.following_inputs import FollowingInputs
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.nominal.field_codec import RootId


class SessionSetupFailed(ValueError):
    """Native session setup is invalid; stop without retrying its input."""


class IndependentExecution(NativeExecution):
    def tracked_launch(self, package: VerifiedNativePackage, **options) -> NativePiRpcLaunch:
        return NativePiRpcLaunch.tracked(package, **options)


@dataclass(kw_only=True)
class SelectedExecution(PackagedNativeExecution):
    max_attempts: ClassVar[int] = RetryableProviderFailure.max_retries + 1

    root: Path
    wire_root_id: RootId
    owner_name: str
    opt_in: bool = True
    after_seq: int = 0
    session_file: Path | None = None
    session_directory: Path = field(init=False)
    action: SelectedAction = field(default_factory=CodingSelectedAction)
    write_authority: SelectedWriteAuthority = field(default_factory=NoSelectedWritePlans)
    _run_permit: threading.Lock = field(init=False, default_factory=threading.Lock)
    following: FollowingInputs = field(default_factory=FollowingInputs, init=False, compare=False)
    def tracked_launch(self, package: VerifiedNativePackage, **options) -> NativePiRpcLaunch:
        """Every stage derives its complete launch from the original acquired artifact."""
        if package != self.native_package:
            raise IdentityConflict("Selected launch differs from its execution package")
        return NativePiRpcLaunch.tracked(package, **options)

    def retained_source(self, session_file: Path):
        """The owner's idle child on this saved source with this package, if still current."""
        try:
            retained = self.pi_session.child_state.idle()
        except NativePiUnavailable:
            return None
        if retained.identity.session_file != str(session_file) or retained.child.key[0].package != self.native_package:
            return None
        return retained

    def validate(self) -> None:
        """Check execution authority; the process already owns its acquired package."""
        if not self.opt_in:
            raise PublicationActivationBlocked("coordinated runtime requires explicit activation")
        try:
            NativeSessionFiles.require_private_directory(self.root)
            owner = wire(self.root).registry.require(self.owner_name)
            if not owner.worktree.path.is_dir():
                raise ValueError("registered participant worktree is unavailable")
            directory = owner.native_session_directory(self.root)
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            NativeSessionFiles.require_private_directory(directory)
            paths = ((self.session_file,) if self.session_file is not None
                     else owner.runtime.read_keys())
            for path in paths:
                NativeSessionFiles.require_private_directory(Path(path).absolute().parent)
        except NativePiUnavailable as error:
            raise SessionSetupFailed(f"Native session setup failed: {error}") from error
        self.session_directory = directory

    async def full(self, participant: SelectedParticipant, session: SelectedSession,
                   snapshot: RecoverySnapshot) -> CoordinatedTurn:
        """Direct work and a FULL verdict share one attempt and tool selection."""
        attempt = await SelectedAttempt.engage(participant, snapshot)
        return await attempt.run(self, session, self.action, self.write_authority)

    async def run(
        self, *, publication: MroDispatch = NoLivePublication(),
        pi_session: PersistentPiSession | None = None,
        following: FollowingInputs | None = None,
    ) -> CoordinatedTurn | None:
        if not self._run_permit.acquire(blocking=False):
            raise IdentityConflict("Selected execution cannot be reused")
        if pi_session is not None:
            self.pi_session = pi_session
        if following is not None:
            self.following = following
        self.root = Path(self.root).absolute()
        # This execution must verify its package before selecting work.
        # Wait for the blocking check before claiming any input.
        await Coordination.run_worker(self.validate)
        comms = wire(self.root)
        with Coordination(str(CoordinationStore.path_in(self.root))) as store:
            while True:
                async with SelectedParticipant.select(
                    comms,
                    store,
                    self.wire_root_id,
                    self.owner_name,
                    self.after_seq,
                    cursor_delivery=self.cursor_delivery,
                    publication=publication,
                ) as participant:
                    if participant is None:
                        return None
                    try:
                        session = await SelectedSession.prepare(
                            participant,
                            self.session_file,
                            self,
                        )
                    except NativePiUnavailable as error:
                        await participant.fail_before_send(self.max_attempts)
                        raise SessionSetupFailed(f"Native session acquisition failed: {error}") from error
                    try:
                        async with session.files(self.root).accept_input():
                            return await SelectedConsideration(participant).run(self, session)
                    except RetryScheduled:
                        # Leaving the selection closes this turn lease. The next
                        # iteration acquires the newer generation required by retry.
                        continue
