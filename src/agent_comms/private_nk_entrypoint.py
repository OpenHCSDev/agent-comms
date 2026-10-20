"""Pinned private N/K owner launch configuration.

The public ACP attachment never becomes a model executor. An owner runs only
with an exact root ID and a reviewed native package, both derived from their
owners for each process. This module installs no marker, participant, schema or input.
"""

from __future__ import annotations

from .launch_runtime import LaunchRuntime

from abc import ABC, abstractmethod
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked

from .coordinator import Coordination
from .native_arguments import NativeArguments
from .native_package import (
    MANIFEST, NativePackageError, VerifiedNativePackage, pinned_native_package,
)
from .native_pi import NativePiUnavailable, _private_session_dir
from .wire_log import WireLog

if TYPE_CHECKING:
    from .acp_extension import CursorScope
    from .active_route import CommsRoute
    from .cursor_publication import CursorPublication
    from .threads import Thread


class RootLaunch(ABC):
    """How a bus root launches native owners, decoded once from its marker where the root
    is opened: a private root launches pinned owners, a shared root launches none."""

    __slots__ = ()

    @staticmethod
    def derive(route: CommsRoute) -> RootLaunch:
        """This route's launch from the owners of its facts.

        The marker owns the root ID and this installation's pin owns the package.
        A parent and its child read the same marker and pin, so they derive the
        same launch.
        """
        root = route.root
        metadata = WireLog(root / "bus.jsonl").read_metadata_unlocked()
        if not metadata.private:
            return SharedRootLaunch(root)
        return PrivateNkLaunch.installed(root, metadata.wire_root_id)

    @property
    @abstractmethod
    def child_root(self) -> Path:
        """The root written into the child's explicit launch record."""

    @abstractmethod
    def require_private(self) -> PrivateNkLaunch:
        """This root's private launch; a shared root refuses."""

    @abstractmethod
    def check(self) -> None:
        """Refuse a launch whose root was replaced; a shared root has nothing to check."""

    @abstractmethod
    def restart_entrypoint(self, source_binary: str, runtime: LaunchRuntime) -> str:
        """The binary a restarted owner runs."""

    @abstractmethod
    def prepare_owner(self, thread: Thread, agent_bin: str, runtime: LaunchRuntime) -> str:
        """Prepare this root for a launched owner; the binary that owner runs."""

    @abstractmethod
    def package_for(self, command: str) -> VerifiedNativePackage:
        """The reviewed package a native execution of ``command`` runs."""

    @abstractmethod
    def cursor_scope(self, publication: CursorPublication, thread_name: str,
                     session_id: str) -> CursorScope:
        """The cursor scope this process may publish for ``thread_name``."""

    @abstractmethod
    async def cursor_metadata(self, publication: CursorPublication, thread_name: str,
                              session_id: str) -> tuple:
        """The trusted cursor metadata a session publishes."""


@dataclass(frozen=True, slots=True)
class SharedRootLaunch(RootLaunch):
    """An ordinary shared root: it launches no private owner and publishes no cursor."""

    root: Path

    @property
    def child_root(self) -> Path:
        """An ordinary root keeps its canonical child path across symlink changes."""
        return self.root.resolve()

    def require_private(self) -> PrivateNkLaunch:
        raise PublicationActivationBlocked("requires a private bus root")

    def check(self) -> None:
        pass

    def restart_entrypoint(self, source_binary: str, runtime: LaunchRuntime) -> str:
        return source_binary

    def prepare_owner(self, thread: Thread, agent_bin: str, runtime: LaunchRuntime) -> str:
        return agent_bin

    def package_for(self, command: str) -> VerifiedNativePackage:
        stack_launcher = MANIFEST.parent / "bin/pi-native"
        if Path(shutil.which(command) or command).resolve() != stack_launcher.resolve():
            raise NativePiUnavailable("Native owner requires a configured pinned package")
        return VerifiedNativePackage.acquire(pinned_native_package())

    def cursor_scope(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> CursorScope:
        return publication.shared_scope()

    async def cursor_metadata(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> tuple:
        return ()


@dataclass(frozen=True, slots=True)
class PrivateNkLaunch(RootLaunch):
    validated_root: Path
    wire_root_id: str
    native_package: VerifiedNativePackage

    @property
    def child_root(self) -> Path:
        """A private child derives the same launch from its validated root."""
        return self.validated_root

    def require_private(self) -> PrivateNkLaunch:
        return self

    def check(self) -> None:
        self.validate()

    def restart_entrypoint(self, source_binary: str, runtime: LaunchRuntime) -> str:
        self.validate()
        return runtime.native_entrypoint

    def prepare_owner(self, thread: Thread, agent_bin: str, runtime: LaunchRuntime) -> str:
        if agent_bin == NativeArguments.command_name:
            # The default stock binary cannot attest native input IDs.
            # Keep the owner on this installation's pinned Pi entrypoint.
            agent_bin = runtime.native_entrypoint
        else:
            # The owner process keeps the launcher selected at its start.
            # A later PATH or installed-runtime symlink change cannot retarget
            # drains already owned by this process.
            agent_bin = str(Path(shutil.which(agent_bin) or agent_bin).resolve(strict=True))
        # Explicit launch installs the current runtime before registering
        # this owner. Readers never create or repair runtime schemas.
        with Coordination(str(self.validated_root / "coordination.sqlite3")) as store:
            store.install_private_runtime()
            store.participants.register(
                thread.incarnation.require_recorded().lookup,
                thread.name,
                thread.name,
                committed=True,
            )
        return agent_bin

    def package_for(self, command: str) -> VerifiedNativePackage:
        return self.validate()

    def cursor_scope(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> CursorScope:
        return publication.live_scope(self.wire_root_id, thread_name, session_id)

    async def cursor_metadata(
        self, publication: CursorPublication, thread_name: str, session_id: str,
    ) -> tuple:
        return await publication.observed_metadata(thread_name, session_id)

    @classmethod
    def installed(cls, root: Path, wire_root_id: str) -> PrivateNkLaunch:
        """A private root's launch with this installation's pinned native package."""
        try:
            package = VerifiedNativePackage.acquire(pinned_native_package())
        except NativePackageError as error:
            raise PublicationActivationBlocked(
                "private owner launch needs this installation's prepared native package"
            ) from error
        return cls(root, wire_root_id, package)

    def validate(self) -> VerifiedNativePackage:
        """Recheck the bus root while keeping this process's chosen native package.

        If the private directory or checked bus root is missing, create no
        directory, registry or database and make no provider request.
        """
        _private_session_dir(self.validated_root)
        with WireLog(self.validated_root / "bus.jsonl").checked_read() as source:
            marker = source.marker
        if marker.root_id != self.wire_root_id:
            raise IdentityConflict("private initial wire root changed")
        return self.native_package
