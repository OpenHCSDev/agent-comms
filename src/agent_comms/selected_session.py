"""Selected sources own identity checks, not which tools a turn carries."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from agent_comms.native_arguments import NativeArguments, NativeInputProofArgument
from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.participant_identity import ParticipantIdentity
from .native_session_reopen import NativeSessionIdentity
from agent_comms.native_pi import NativePiUnavailable, _session_location, _durable_private_session_dir
from .private_path import NativeSessionFileRole
from .launch_runtime import RestartEnvironment, ThreadLaunch
from .pi_vocabulary import ThinkingChoice

if TYPE_CHECKING:
    from .selected_participant import SelectedParticipant


@dataclass(frozen=True)
class SelectedSession:
    """Hold the chosen native session before writing its first input.

    The directory is reserved for launch files and tools. A previously selected
    saved journal may live elsewhere.
    """
    directory: Path

    @property
    def path(self) -> Path | None:
        return None

    @property
    def session_file(self) -> str | None:
        """Wire projection of the original selected source, not another field."""
        return None

    def attestation(self):
        from .native_attestation import PendingAttestation

        return PendingAttestation()

    def tracked_attestation(self, current):
        return current

    def launch_arguments(self, thinking_level: type[ThinkingChoice]) -> tuple[str, ...]:
        return NativeArguments(()).with_thinking(thinking_level).argv

    def launch_environment(self, agent_dir: Path, original) -> tuple[dict[str, str], RestartEnvironment]:
        environment = ThreadLaunch.strip(dict(os.environ))
        environment.update(original or {})
        configuration = RestartEnvironment.inherit(environment).for_agent(agent_dir)
        environment["PI_OFFLINE"] = "1"
        return environment, configuration

    def attest(self, identity: NativeSessionIdentity) -> Path:
        # Only an unselected new session uses the originally allocated directory.
        return _session_location(self.directory, identity.session_file)

    def verify_start(self, actual: NativeSessionIdentity) -> Path:
        return self.attest(actual)

    def require_context(self, context) -> None:
        self.attest(NativeSessionIdentity(context.session_id, str(context.session_file)))

    def continued(self, context) -> SelectedSession:
        raise NativePiUnavailable("An unselected session cannot own a tracked native continuation")

    def files(self, root: Path) -> NativeSessionFiles:
        raise NativePiUnavailable("An unselected session has no bus-owned proof")

    def proof_arguments(self, root: Path) -> tuple[str, ...]:
        return ()

    @classmethod
    def for_launch(
        cls, directory: Path, path: Path | None, *, root: Path,
        worktree: Path, owner: ParticipantIdentity,
    ) -> SelectedSession:
        identity = (create_fresh_private_session(root, worktree=worktree) if path is None
                    else NativeSessionIdentity.locate(str(path)))
        return SavedSelectedSession(directory, owner=owner, identity=identity)

    @classmethod
    async def prepare(
        cls,
        participant: SelectedParticipant,
        path: Path | None,
        execution,
    ) -> SelectedSession:
        """Borrow the original execution's acquired artifact for its header read.

        SelectedExecution validates before selecting this participant. This
        joined preparation shares that immutable acquisition; standalone
        for_launch remains a separate verified lookup.
        """
        def acquire() -> SelectedSession:
            directory = execution.session_directory
            thread = participant.owner.thread
            runtime = thread.runtime if path is None else thread.backend.location_type(str(path))
            return runtime.select_session(directory, execution, source_reads,
                                          root=participant.comms.root, thread=thread)

        with participant.cursor_delivery.measurements.operation("session_reopen"):
            async with participant.cursor_delivery.reading() as source_reads:
                return await Coordination.run_worker(acquire)


@dataclass(frozen=True)
class SavedSelectedSession(SelectedSession):
    """Continue the exact captured native identity, irrespective of storage parent."""
    identity: NativeSessionIdentity = field(kw_only=True)
    owner: ParticipantIdentity = field(kw_only=True)

    def files(self, root: Path) -> NativeSessionFiles:
        return NativeSessionFiles(self.path, root=root, owner=self.owner)

    def proof_arguments(self, root: Path) -> tuple[str, ...]:
        proof = self.files(root).input_proof
        _durable_private_session_dir(proof.parent)
        return NativeInputProofArgument(str(proof)).argv

    def continued(self, context) -> SelectedSession:
        self.require_context(context)
        return SavedSelectedSession(self.directory, owner=self.owner,
            identity=NativeSessionIdentity(context.session_id, str(context.session_file)))

    @property
    def path(self) -> Path:
        return self.identity.path

    @property
    def session_file(self) -> str:
        return self.identity.session_file

    def attestation(self):
        from .native_attestation import PendingAttestation

        return PendingAttestation(self.identity)

    def tracked_attestation(self, current):
        from .native_attestation import KnownSessionAttestation

        return KnownSessionAttestation(self.identity)

    def attest(self, identity: NativeSessionIdentity) -> Path:
        self.identity.require_same_session(identity)
        return self.path

    def launch_arguments(self, thinking_level: type[ThinkingChoice]) -> tuple[str, ...]:
        try:
            self.verify_start(self.identity)
        except IdentityConflict as error:
            raise NativePiUnavailable(str(error)) from error
        return super().launch_arguments(thinking_level)

    def verify_start(self, actual: NativeSessionIdentity) -> Path:
        self.identity.require_same_session(actual)
        try:
            if self.path.resolve(strict=True) != self.path:
                raise ValueError("redirected saved session")
            NativeSessionFileRole.require(self.path.lstat())
        except (OSError, ValueError) as error:
            raise IdentityConflict("native start requires the exact saved session") from error
        return self.path

    def require_context(self, context) -> None:
        self.identity.require_context(context)
