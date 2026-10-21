"""One captured batch, one exact lease, no automatic native input replay.

Preparation owns registry/participant/source identity; session enrollment owns
first-start authority; each reserved stage owns its settlement. The runner only
orders those lifetimes and cannot represent a half-initialized native attempt.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .agent_events import CompactionEvent
from .comms import Comms
from .coordination_database import CoordinationStore
from .coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    RetryScheduled,
)
from .coordination_snapshot import RecoverySnapshot
from .coordinator import Coordination
from .cursor_publication import CursorDelivery
from .following_inputs import FollowingInputs
from .native_package import VerifiedNativePackage
from .native_pi import NativePiRpcLaunch
from .native_failures import NativePiUnavailable
from agent_comms.native_session_files import NativeSessionFiles
from .pi_native_backend import PersistentPiSession
from .selected_actions import CodingSelectedAction, SelectedAction
from .selected_participant import SelectedParticipant
from .selected_result import CoordinatedTurn
from .selected_session import SelectedSession
from .selected_tool_broker import SelectedToolIntent
from .selected_turn import SelectedAttempt, SelectedConsideration
from .selected_write_authority import NoSelectedWritePlans, SelectedWriteAuthority
from .turn_failure import RetryableProviderFailure


class SessionSetupFailed(ValueError):
    """Native session setup is invalid; stop without retrying its input."""


@dataclass(kw_only=True)
class NativeExecution(ABC):
    cursor_delivery: CursorDelivery
    pi_session: PersistentPiSession = field(default_factory=PersistentPiSession, compare=False)

    @abstractmethod
    def tracked_launch(self, package: VerifiedNativePackage, **options) -> NativePiRpcLaunch: ...


class IndependentExecution(NativeExecution):
    def tracked_launch(self, package: VerifiedNativePackage, **options) -> NativePiRpcLaunch:
        return NativePiRpcLaunch.tracked(package, **options)


@dataclass(kw_only=True)
class SelectedExecution(NativeExecution):
    max_attempts: ClassVar[int] = RetryableProviderFailure.max_retries + 1

    root: Path
    wire_root_id: str
    owner_name: str
    native_package: VerifiedNativePackage
    opt_in: bool = True
    after_seq: int = 0
    session_file: Path | None = None
    session_directory: Path = field(init=False)
    selected_existing_file_write: SelectedAction | None = None
    selected_tool_intent: SelectedToolIntent | None = None
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
            owner = Comms(self.root).registry.require(self.owner_name)
            if not Path(owner.worktree).is_dir():
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
        if self.selected_tool_intent is not None:
            if type(self.selected_tool_intent) is not SelectedToolIntent:
                raise TypeError("selected tool requires a nominal owner intent")
            if self.selected_existing_file_write is not None:
                raise IdentityConflict("selected tool cannot share an operator file plan")

    def action(self) -> SelectedAction:
        if self.selected_tool_intent is not None:
            return self.selected_tool_intent
        if self.selected_existing_file_write is not None:
            return self.selected_existing_file_write
        return CodingSelectedAction()

    async def full(self, participant: SelectedParticipant, session: SelectedSession,
                   snapshot: RecoverySnapshot) -> CoordinatedTurn:
        """Direct work and a FULL verdict share one attempt and tool selection."""
        attempt = await SelectedAttempt.engage(participant, snapshot)
        return await attempt.run(self, session, self.action(), self.write_authority)

    async def run(
        self, *, on_compaction: Callable[[CompactionEvent], Awaitable[None]] | None = None,
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
        comms = Comms(self.root)
        with Coordination(str(CoordinationStore.path_in(self.root))) as store:
            while True:
                async with SelectedParticipant.select(
                    comms,
                    store,
                    self.wire_root_id,
                    self.owner_name,
                    self.after_seq,
                    cursor_delivery=self.cursor_delivery,
                    on_compaction=on_compaction,
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
                        return await SelectedConsideration(participant).run(self, session)
                    except RetryScheduled:
                        # Leaving the selection closes this turn lease. The next
                        # iteration acquires the newer generation required by retry.
                        continue
