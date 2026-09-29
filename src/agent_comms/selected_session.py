"""Selected session enrollment owns first-start capability, never reconstructs it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .compaction_journal import CompactionJournal
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_response import _response_boundary
from .errors import RelationViolationError
from .fresh_private_session import FreshPrivateSession, create_fresh_private_session
from .maintenance_barrier import MaintenanceBarrier
from .native_input_owner import RegistryOwner
from .selected_actions import CodingSelectedAction, NoSelectedTools, SelectedAction

if TYPE_CHECKING:
    from .selected_participant import SelectedParticipant


@dataclass(frozen=True)
class SelectedSession:
    directory: Path
    path: Path | None = None
    creation: FreshPrivateSession | None = None

    def default_action(self) -> SelectedAction:
        return CodingSelectedAction()

    def startup(self) -> FreshPrivateSession | None:
        return None

    def continued(self, path: Path) -> SelectedSession:
        # A returned live input consumes first-start authority. Creation coverage
        # stays with the result; reopening the path cannot mint another grant.
        return SelectedSession(self.directory, path, self.creation)

    @classmethod
    def prepare(
        cls,
        participant: SelectedParticipant,
        path: Path | None,
        fresh: bool,
        thinking_level: str | None,
    ) -> SelectedSession:
        directory = participant.comms.root / "native-sessions" / participant.lookup
        worktree = Path(participant.owner.thread.worktree).absolute()
        if not worktree.is_dir():
            raise IdentityConflict("registered participant worktree is unavailable")
        if not fresh:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            if path is not None:
                path = Path(path).absolute()
                if path.parent != directory:
                    raise IdentityConflict("native session is outside the selected recipient")
            return cls(directory, path)
        # Original wire→bus→registry→store→journal order spans exclusive file
        # creation, fsync and enrollment. No historical-file coverage inference.
        with _response_boundary(participant.bus) as registry, participant.store.session.read():
            actual = RegistryOwner.capture(
                registry,
                participant.owner.thread.name,
                "fresh-session owner changed before enrollment",
            )
            try:
                actual.require_exact(
                    registry, participant.owner.thread, participant.owner.admission_generation
                )
            except RelationViolationError as error:
                raise StaleFence("fresh-session owner changed before enrollment") from error
            participant.identity.require(participant.store, participant.lookup)
            MaintenanceBarrier(participant.bus._registry.store.path).assert_open_unlocked()
            creation = create_fresh_private_session(
                directory,
                worktree=worktree,
                selected_thinking_level=thinking_level,
            )
            CompactionJournal(
                participant.comms.root / "compaction-commits.sqlite3"
            ).private_inputs.enroll(
                creation,
                incarnation=participant.owner.thread.incarnation,
                owner_lookup=participant.lookup,
                owner_generation=participant.identity.generation,
                admission_generation=participant.owner.admission_generation,
            )
        if thinking_level is not None:
            return FirstSelectedSession(directory, creation.path, creation=creation)
        return cls(directory, creation.path, creation)


@dataclass(frozen=True)
class FirstSelectedSession(SelectedSession):
    creation: FreshPrivateSession = field(kw_only=True)

    def default_action(self) -> SelectedAction:
        return NoSelectedTools()

    def startup(self) -> FreshPrivateSession:
        return self.creation
