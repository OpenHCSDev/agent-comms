"""One sealed assignment, one exact lease, no automatic native input replay.

Preparation owns registry/participant/source identity; session enrollment owns
first-start authority; each reserved stage owns its settlement. The runner only
orders those lifetimes and cannot represent a half-initialized native attempt.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from .comms import Comms
from .coordination_errors import IdentityConflict, PublicationActivationBlocked
from .coordinator import Coordination
from .native_pi import _private_session_dir, _trusted_package
from .selected_actions import SelectedAction, SelectedExistingFileWrite
from .selected_participant import SelectedParticipant
from .selected_result import CoordinatedTurn
from .selected_session import SelectedSession
from .selected_tool_broker import SelectedToolIntent
from .selected_turn import SelectedAttempt, SelectedConsideration
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
    _run_permit: threading.Lock = field(init=False, default_factory=threading.Lock)

    def validate(self) -> None:
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
            if self.selected_thinking_level not in ("low", "high"):
                raise IdentityConflict(
                    "Selected level requires explicitly supported fresh enrollment"
                )
        _trusted_package(self.native_package)  # before any claim or native reservation
        if self.selected_existing_file_write is not None:
            if type(self.selected_existing_file_write) is not SelectedExistingFileWrite:
                raise TypeError("selected write requires an explicit trusted plan")
        if self.selected_tool_intent is not None:
            if type(self.selected_tool_intent) is not SelectedToolIntent:
                raise TypeError("selected tool requires a nominal owner intent")
            if self.selected_existing_file_write is not None:
                raise IdentityConflict("selected tool cannot share an operator file plan")

    def action(self, session: SelectedSession) -> SelectedAction:
        if self.selected_tool_intent is not None:
            return self.selected_tool_intent
        if self.selected_existing_file_write is not None:
            return self.selected_existing_file_write
        return session.default_action()

    async def run(self) -> CoordinatedTurn | None:
        if not self._run_permit.acquire(blocking=False):
            raise IdentityConflict("Selected execution cannot be reused")
        self.root = Path(self.root).absolute()
        self.validate()
        comms = Comms(self.root)
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
            with SelectedParticipant.select(
                comms,
                store,
                self.wire_root_id,
                self.owner_name,
                self.after_seq,
            ) as participant:
                if participant is None:
                    return None
                session = SelectedSession.prepare(
                    participant,
                    self.session_file,
                    self.fresh_private_enrollment,
                    self.selected_thinking_level,
                )
                session, ignored = await SelectedConsideration(participant).run(
                    self.native_package, session
                )
                if ignored is not None:
                    return ignored
                attempt = SelectedAttempt.engage(participant)
                return await attempt.run(
                    self.native_package, session, self.action(session), self.write_authority
                )
