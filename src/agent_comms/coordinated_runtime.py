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

from .agent_backend import AgentBackend
from .agent_events import CompactionEvent
from .comms import Comms
from .coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    RetryScheduled,
)
from .coordination_snapshot import RecoverySnapshot
from .coordinator import Coordination
from .following_inputs import FollowingInputs
from .native_pi import NativePiRpcLaunch, _private_session_dir
from .selected_actions import CodingSelectedAction, SelectedAction
from .selected_participant import SelectedParticipant
from .selected_result import CoordinatedTurn
from .selected_session import SelectedSession
from .selected_tool_broker import SelectedToolIntent
from .selected_turn import SelectedAttempt, SelectedConsideration
from .selected_write_authority import NoSelectedWritePlans, SelectedWriteAuthority


@dataclass(kw_only=True)
class NativeExecution(ABC):
    backend: AgentBackend = field(compare=False)

    @abstractmethod
    def tracked_launch(self, package: Path, **options) -> NativePiRpcLaunch: ...


class IndependentExecution(NativeExecution):
    def tracked_launch(self, package: Path, **options) -> NativePiRpcLaunch:
        return NativePiRpcLaunch.tracked(package, **options)


@dataclass(kw_only=True)
class SelectedExecution(NativeExecution):
    max_attempts: ClassVar[int] = 3

    root: Path
    wire_root_id: str
    owner_name: str
    native_package: Path
    opt_in: bool = True
    after_seq: int = 0
    session_file: Path | None = None
    selected_existing_file_write: SelectedAction | None = None
    selected_tool_intent: SelectedToolIntent | None = None
    write_authority: SelectedWriteAuthority = field(default_factory=NoSelectedWritePlans)
    # The worker verifies its pinned package once (packages never change in
    # place) and hands every run that one verified launcher, only when needed.
    verified_launcher: Callable[[], Callable[..., NativePiRpcLaunch]] | None = None
    _run_permit: threading.Lock = field(init=False, default_factory=threading.Lock)
    following: FollowingInputs = field(default_factory=FollowingInputs, init=False, compare=False)
    _tracked_factory: Callable[..., NativePiRpcLaunch] | None = field(
        init=False, default=None, repr=False, compare=False,
    )

    def tracked_launch(self, package: Path, **options) -> NativePiRpcLaunch:
        """Consume the original pre-claim acquisition for each stage's launch.

        Every stage rebuilds its complete source/configuration through the
        executable factory acquired by validate before selecting a participant;
        no native child, input proof or readiness is borrowed from a prior turn.
        """
        if package != self.native_package:
            raise IdentityConflict("Selected launch differs from its execution package")
        if self._tracked_factory is None:
            raise IdentityConflict("Selected launch precedes its execution package acquisition")
        return self._tracked_factory(**options)

    def validate(self) -> None:
        """Acquire this execution's package once; claim acceptance and launch share it."""
        if self._tracked_factory is not None:
            return
        if not self.opt_in:
            raise PublicationActivationBlocked("coordinated runtime requires explicit activation")
        _private_session_dir(self.root)
        if self.selected_tool_intent is not None:
            if type(self.selected_tool_intent) is not SelectedToolIntent:
                raise TypeError("selected tool requires a nominal owner intent")
            if self.selected_existing_file_write is not None:
                raise IdentityConflict("selected tool cannot share an operator file plan")
        if self.verified_launcher is None:
            self._tracked_factory = NativePiRpcLaunch.acquire_tracked(self.native_package)
            return
        launcher = self.verified_launcher()
        if launcher.args[0] != Path(self.native_package).absolute():
            raise IdentityConflict("Worker's verified launcher is for a different package")
        self._tracked_factory = launcher

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
        following: FollowingInputs | None = None,
    ) -> CoordinatedTurn | None:
        if not self._run_permit.acquire(blocking=False):
            raise IdentityConflict("Selected execution cannot be reused")
        if following is not None:
            self.following = following
        self.root = Path(self.root).absolute()
        # This execution must verify its package before selecting work.
        # Wait for the blocking check before claiming any input.
        await Coordination.run_worker(self.validate)
        comms = Comms(self.root)
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
            while True:
                async with SelectedParticipant.select(
                    comms,
                    store,
                    self.wire_root_id,
                    self.owner_name,
                    self.after_seq,
                    on_compaction=on_compaction,
                ) as participant:
                    if participant is None:
                        return None
                    session = await SelectedSession.prepare(
                        participant,
                        self.session_file,
                        self,
                    )
                    try:
                        return await SelectedConsideration(participant).run(self, session)
                    except RetryScheduled:
                        # Leaving the selection closes this turn lease. The next
                        # iteration acquires the newer generation required by retry.
                        continue
