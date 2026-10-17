"""Selected sources own identity checks, not which tools a turn carries."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from .native_session_reopen import NativeSessionIdentity, SessionIdentityHelper
from .native_pi import _session_location
from .launch_runtime import RestartEnvironment

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution
    from .selected_participant import SelectedParticipant


@dataclass(frozen=True)
class SelectedSession:
    """Selected native source before its first original input is written.

    directory is an acquired private launch/tool resource. It is not a claim
    that an already selected saved journal lives in that directory.
    """
    directory: Path

    @property
    def path(self) -> Path | None:
        return None

    def attestation(self):
        from .native_attestation import PendingAttestation

        return PendingAttestation()

    def tracked_attestation(self, current):
        return current

    def launch_arguments(self, thinking_level: str | None) -> tuple[str, ...]:
        return ("--thinking", thinking_level) if thinking_level is not None else ()

    def launch_environment(self, agent_dir: Path, original) -> tuple[dict[str, str], RestartEnvironment]:
        environment = dict(os.environ)
        for name in (
            "PI_PARENT_ID", "PI_AGENT_TAGS", RestartEnvironment.owner_key,
            "AGENT_COMMS_TAGS", "AGENT_COMMS_SELECTED_TOOL_SOCKET",
            "AGENT_COMMS_SELECTED_TOOL_TOKEN",
        ):
            environment.pop(name, None)
        environment.update(original or {})
        configuration = RestartEnvironment.inherit(environment).for_agent(agent_dir)
        environment["PI_OFFLINE"] = "1"
        return environment, configuration

    def attest(self, identity: NativeSessionIdentity) -> Path:
        # Only an unselected new session uses the originally allocated directory.
        return _session_location(self.directory, identity.source_path)

    def verify_start(self, actual: NativeSessionIdentity) -> Path:
        return self.attest(actual)

    def require_context(self, context) -> None:
        self.attest(NativeSessionIdentity(context.session_id, str(context.source_path)))

    def continued(self, context) -> SelectedSession:
        self.require_context(context)
        return SavedSelectedSession(self.directory,
            identity=NativeSessionIdentity(context.session_id, str(context.source_path)))

    @classmethod
    def for_launch(cls, directory: Path, path: Path | None, package: Path) -> SelectedSession:
        if path is None:
            return cls(directory)
        return SavedSelectedSession(directory,
            identity=NativeSessionIdentity.locate(package, str(path)))

    @classmethod
    async def prepare(
        cls,
        participant: SelectedParticipant,
        path: Path | None,
        execution: SelectedExecution,
    ) -> SelectedSession:
        """Borrow the original execution's acquired artifact for its header read.

        SelectedExecution validates before selecting this participant. This
        joined preparation shares that immutable acquisition; standalone
        for_launch remains a separate verified lookup.
        """
        def acquire() -> SelectedSession:
            directory = participant.comms.root / "native-sessions" / participant.lookup
            worktree = Path(participant.owner.thread.worktree).absolute()
            if not worktree.is_dir():
                raise IdentityConflict("registered participant worktree is unavailable")
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            thread = participant.owner.thread
            runtime = thread.runtime if path is None else thread.backend.location_type(str(path))
            return runtime.select_session(directory, execution)

        return await Coordination.run_worker(acquire)


@dataclass(frozen=True)
class SavedSelectedSession(SelectedSession):
    """Continue the exact captured native identity, irrespective of storage parent."""
    identity: NativeSessionIdentity = field(kw_only=True)

    @property
    def path(self) -> Path:
        return self.identity.path

    def attestation(self):
        from .native_attestation import PendingAttestation

        return PendingAttestation(self.identity)

    def tracked_attestation(self, current):
        from .native_attestation import KnownSessionAttestation

        return KnownSessionAttestation(self.identity)

    def attest(self, identity: NativeSessionIdentity) -> Path:
        self.identity.require_same_session(identity)
        return self.path

    def verify_start(self, actual: NativeSessionIdentity) -> Path:
        self.identity.require_same_session(actual)
        try:
            if self.path.resolve(strict=True) != self.path or not self.path.is_file():
                raise IdentityConflict("native start requires the exact saved session")
        except OSError as error:
            raise IdentityConflict("native start requires the exact saved session") from error
        return self.path

    def require_context(self, context) -> None:
        self.identity.require_context(context)
