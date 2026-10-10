"""Selected session enrollment owns first-start capability, never reconstructs it."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict, StaleFence
from .coordination_response import _response_boundary
from .coordinator import Coordination
from .errors import RelationViolationError
from .diagnostics import PublicationMeasurements
from .fresh_private_session import FreshPrivateSession, create_fresh_private_session
from .maintenance_barrier import MaintenanceBarrier
from .native_session_reopen import NativeSessionIdentity, SessionIdentityHelper
from .native_pi import NativePiUnavailable, _session_location
from .owner_launch import RestartEnvironment
from .selected_actions import CodingSelectedAction, NoSelectedTools, SelectedAction

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution
    from .pi_native_backend import PersistentPiSession
    from .selected_participant import SelectedParticipant


@dataclass(frozen=True)
class SelectedSession:
    """Selected native source before its first original input is written.

    directory is an acquired private launch/tool resource. It is not a claim
    that an already selected saved journal lives in that directory.
    """
    directory: Path
    creation: FreshPrivateSession | None = field(default=None, kw_only=True)
    # The owner's native custody for this source. Custody only; it never takes
    # part in the launch key.
    custody: PersistentPiSession | None = field(default=None, kw_only=True, compare=False, repr=False)

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

    def startup_admission(self, launch, root, boundary, *,
                          measurements: PublicationMeasurements | None = None):
        from .native_startup import NativeStartupAdmission

        return NativeStartupAdmission.for_launch(launch, root=root, measurements=measurements)

    def default_action(self) -> SelectedAction:
        return CodingSelectedAction()

    def launch_arguments(self, thinking_level: str | None) -> tuple[str, ...]:
        return ("--thinking", thinking_level) if thinking_level is not None else ()

    def require_launch_tools(self, mode) -> None:
        pass

    def require_launch_header(self) -> None:
        pass

    def launch_environment(self, agent_dir: Path, original) -> tuple[dict[str, str], RestartEnvironment]:
        environment = dict(os.environ)
        for name in (
            "PI_AGENT_ID", "PI_PARENT_ID", "PI_AGENT_TAGS", "AGENT_COMMS_THREAD",
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
        return _session_location(self.directory, identity.session_file)

    def admit(self, actual: NativeSessionIdentity, runtime_revision) -> Path:
        # get_state established the generated path before the original writer.
        return self.attest(actual)

    def require_context(self, context) -> None:
        self.attest(NativeSessionIdentity(context.session_id, str(context.session_file)))

    def continued(self, context) -> SelectedSession:
        self.require_context(context)
        return SavedSelectedSession(self.directory,
            identity=NativeSessionIdentity(context.session_id, str(context.session_file)),
            creation=self.creation, custody=self.custody)

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
        fresh: bool,
        thinking_level: str | None,
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
            if not fresh:
                directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                # Captured registry intent selects ordinary continuation. An explicit
                # operator selection remains explicit; no downstream reader guesses it.
                selected = path if path is not None else participant.owner.thread.session_file
                if selected is None:
                    return cls(directory)
                # The owner's idle child already attested this exact saved source
                # and the file is unchanged since; no helper re-reads its history.
                retained = execution.retained_source(Path(selected).absolute())
                if retained is not None:
                    return SavedSelectedSession(directory, identity=retained.identity,
                                                custody=execution.pi_session)
                return SavedSelectedSession(directory, custody=execution.pi_session,
                                            identity=SessionIdentityHelper.locate(
                    execution.native_package, str(Path(selected).absolute()),
                ))
            # Original wire→bus→registry→store→journal order spans exclusive file
            # creation, fsync and enrollment. No historical-file coverage inference.
            with (
                _response_boundary(participant.bus) as registry,
                Coordination(str(participant.store.session.path)) as resource,
                resource.session.read(),
            ):
                try:
                    participant.owner.require_snapshot(
                        registry, "fresh-session owner changed before enrollment"
                    )
                except RelationViolationError as error:
                    raise StaleFence("fresh-session owner changed before enrollment") from error
                participant.identity.require(resource, participant.lookup)
                MaintenanceBarrier(participant.bus._registry.store.path).assert_open_unlocked()
                creation = create_fresh_private_session(
                    directory,
                    worktree=worktree,
                    selected_thinking_level=thinking_level,
                )
            if thinking_level is not None:
                return FirstSelectedSession(directory,
                    identity=NativeSessionIdentity(creation.session_id, str(creation.path)), creation=creation)
            return SavedSelectedSession(directory,
                identity=NativeSessionIdentity(creation.session_id, str(creation.path)), creation=creation)

        return await Coordination.run_worker(acquire)


@dataclass(frozen=True)
class SavedSelectedSession(SelectedSession):
    """Continue the exact captured native identity, irrespective of storage parent."""
    identity: NativeSessionIdentity = field(kw_only=True)

    @property
    def path(self) -> Path:
        return self.identity.path

    @property
    def session_file(self) -> str:
        return self.identity.session_file

    def attestation(self):
        from .native_attestation import PendingAttestation

        return PendingAttestation(self.identity)

    def require_launch_header(self) -> None:
        if self.custody is not None:
            try:
                retained = self.custody.custody.idle()
            except NativePiUnavailable:
                retained = None
            if retained is not None and retained.identity.same_session(self.identity):
                return  # Its live child read this header at launch; the file has not changed since.
        FreshPrivateSession.require_launch_header(self.path, None)

    def attest(self, identity: NativeSessionIdentity) -> Path:
        self.identity.require_same_session(identity)
        return self.path

    def admit(self, actual: NativeSessionIdentity, runtime_revision) -> Path:
        self.identity.require_same_session(actual)
        return self.path

    def require_context(self, context) -> None:
        self.identity.require_context(context)


@dataclass(frozen=True)
class FirstSelectedSession(SavedSelectedSession):
    creation: FreshPrivateSession = field(kw_only=True)

    def default_action(self) -> SelectedAction:
        return NoSelectedTools()

    def startup_admission(self, launch, root, boundary, *,
                          measurements: PublicationMeasurements | None = None):
        from .native_startup import SelectedNativeStartupAdmission

        return SelectedNativeStartupAdmission(root, self.creation, boundary, measurements=measurements)

    def launch_arguments(self, thinking_level: str | None) -> tuple[str, ...]:
        return (
            "--thinking", self.creation.selected_thinking_level,
            "--no-extensions", "--no-skills", "--no-context-files",
            "--no-prompt-templates", "--no-themes",
        )

    def require_launch_tools(self, mode) -> None:
        if mode is not None:
            raise NativePiUnavailable("Selected fresh source cannot launch a file tool")

    def require_launch_header(self) -> None:
        self.creation.verify_selected_startup()
        FreshPrivateSession.require_launch_header(self.path, self.creation.selected_thinking_level)

    def launch_environment(self, agent_dir: Path, original) -> tuple[dict[str, str], RestartEnvironment]:
        # Original selected-source first-start isolation is a distinct leaf
        # capability, not the configuration of ordinary saved continuation.
        if os.name != "posix":
            raise NativePiUnavailable("Selected source requires reviewed POSIX isolation")
        import pwd

        username = pwd.getpwuid(os.geteuid()).pw_name
        environment = {
            "HOME": str(agent_dir), "USER": username, "LOGNAME": username,
            "PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": str(self.directory),
            "PI_OFFLINE": "1", "AGENT_COMMS_SELECTED_SOURCE_COPY": "1",
        }
        configuration = RestartEnvironment(
            home=str(agent_dir), native_config=agent_dir, agent_directory=agent_dir
        )
        return environment, configuration

    def admit(self, actual: NativeSessionIdentity, runtime_revision) -> Path:
        saved = super().admit(actual, runtime_revision)
        if self.creation.verify_selected_startup() != runtime_revision:
            raise NativePiUnavailable("selected fresh source changed before native send")
        return saved
