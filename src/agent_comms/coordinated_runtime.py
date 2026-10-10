"""One captured batch, one exact lease, no automatic native input replay.

Preparation owns registry/participant/source identity; session enrollment owns
first-start authority; each reserved stage owns its settlement. The runner only
orders those lifetimes and cannot represent a half-initialized native attempt.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .pi_vocabulary import ThinkingLevel
from .agent_events import CompactionEvent
from .backend import PersistentPiSession
from .comms import Comms
from .coordination_errors import IdentityConflict, PublicationActivationBlocked
from .coordinator import Coordination
from .native_pi import NativePiRpcLaunch, NativePiUnavailable, _private_session_dir
from .selected_actions import SelectedAction, SelectedExistingFileWrite
from .selected_participant import SelectedParticipant
from .selected_result import CoordinatedTurn
from .selected_session import SelectedSession
from .selected_tool_broker import SelectedToolIntent
from .selected_turn import SelectedConsideration
from .selected_write_authority import NoSelectedWritePlans, SelectedWriteAuthority


@dataclass(kw_only=True)
class SelectedExecution:
    root: Path
    wire_root_id: str
    owner_name: str
    native_package: Path
    opt_in: bool = True
    after_seq: int = 0
    session_file: Path | None = None
    fresh_private_enrollment: bool = False
    selected_thinking_level: str | None = None
    selected_existing_file_write: SelectedExistingFileWrite | None = None
    selected_tool_intent: SelectedToolIntent | None = None
    write_authority: SelectedWriteAuthority = field(default_factory=NoSelectedWritePlans)
    # The worker verifies its pinned package once (packages never change in
    # place) and hands every run that one verified launcher, only when needed.
    verified_launcher: Callable[[], Callable[..., NativePiRpcLaunch]] | None = None
    _run_permit: threading.Lock = field(init=False, default_factory=threading.Lock)
    # The owner session's native custody; None gives each input its own child.
    native_custody: PersistentPiSession | None = field(default=None, init=False, compare=False)
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

    def retained_source(self, session_file: Path):
        """The owner's idle child on this saved source with this package, if still current."""
        if self.native_custody is None:
            return None
        try:
            retained = self.native_custody.custody.idle()
        except NativePiUnavailable:
            return None
        if retained.identity.session_file != str(session_file) or retained.child.key[0].package != self.native_package:
            return None
        return retained

    def validate(self) -> None:
        """Acquire this execution's package once; claim acceptance and launch share it."""
        if self._tracked_factory is not None:
            return
        if not self.opt_in:
            raise PublicationActivationBlocked("coordinated runtime requires explicit activation")
        _private_session_dir(self.root)
        if type(self.fresh_private_enrollment) is not bool:
            raise IdentityConflict("Fresh private enrollment requires an explicit boolean")
        if self.fresh_private_enrollment and self.session_file is not None:
            raise IdentityConflict("Fresh private enrollment requires a new, explicit session")
        if self.selected_thinking_level is not None:
            if not self.fresh_private_enrollment:
                raise IdentityConflict(
                    "Selected level requires explicitly supported fresh enrollment"
                )
            if type(
                self.selected_thinking_level
            ) is not str or not ThinkingLevel.supports_selected(self.selected_thinking_level):
                raise IdentityConflict(
                    "Selected level requires explicitly supported fresh enrollment"
                )
        if (
            self.selected_existing_file_write is not None
            and type(self.selected_existing_file_write) is not SelectedExistingFileWrite
        ):
            raise TypeError("selected write requires an explicit trusted plan")
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

    def action(self, session: SelectedSession) -> SelectedAction:
        if self.selected_tool_intent is not None:
            return self.selected_tool_intent
        if self.selected_existing_file_write is not None:
            return self.selected_existing_file_write
        return session.default_action()

    async def run(
        self, *, on_compaction: Callable[[CompactionEvent], Awaitable[None]] | None = None,
        native_custody: PersistentPiSession | None = None,
    ) -> CoordinatedTurn | None:
        if not self._run_permit.acquire(blocking=False):
            raise IdentityConflict("Selected execution cannot be reused")
        self.native_custody = native_custody
        self.root = Path(self.root).absolute()
        # Package acquisition belongs to this execution before it can select
        # a claim. Join its blocking verification before that custody advances.
        await Coordination.run_worker(self.validate)
        comms = Comms(self.root)
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
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
                    self.fresh_private_enrollment,
                    self.selected_thinking_level,
                    self,
                )
                return await SelectedConsideration(participant).run(self, session)
