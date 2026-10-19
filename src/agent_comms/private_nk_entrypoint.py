"""Pinned private N/K owner launch configuration.

The public ACP attachment never becomes a model executor. An owner runs only
with an exact root ID and a reviewed native package, both derived from their
owners for each process. This module installs no marker, participant, schema or input.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked

from .native_package import NativePackageError, VerifiedNativePackage, pinned_native_package
from .native_pi import _private_session_dir
from .wire_log import WireLog

if TYPE_CHECKING:
    from .cursor_publication import CursorPublication, CursorScope
    from .launch_runtime import LaunchRuntime
    from .threads import Thread


class RootLaunch(ABC):
    """How a bus root launches native owners, decoded once from its marker where the root
    is opened: a private root launches pinned owners, a shared root launches none."""

    __slots__ = ()

    @staticmethod
    def derive(root: Path | str | None = None) -> RootLaunch:
        """This root's launch from the owners of its facts.

        Without a root, the process's selected route names it. The marker owns
        the root ID and this installation's pin owns the package. A parent and
        its child read the same marker and pin, so they derive the same launch.
        """
        from .active_route import resolve_comms_route

        root = resolve_comms_route(root).root
        metadata = WireLog(root / "bus.jsonl").read_metadata_unlocked()
        if not metadata.private:
            return SharedRootLaunch(root)
        return PrivateNkLaunch.installed(root, metadata.wire_root_id)

    @property
    @abstractmethod
    def child_root(self) -> Path:
        """The root a launched child names in ``AGENT_COMMS_ROOT``."""

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
        from .native_package import MANIFEST
        from .native_pi import NativePiUnavailable

        if Path(shutil.which(command) or command).resolve() != (MANIFEST.parent / "bin/pi-native").resolve():
            raise NativePiUnavailable("Native owner requires a configured pinned package")
        return VerifiedNativePackage.acquire(pinned_native_package())

    def cursor_scope(self, publication, thread_name, session_id):
        from .cursor_publication import NoCursorScope

        return NoCursorScope()


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
        if agent_bin == "pi":
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
        from .coordinator import Coordination

        with Coordination(str(self.validated_root / "coordination.sqlite3")) as store:
            store.install_private_runtime()
            store.participants.register(
                thread.incarnation.lookup,
                thread.name,
                thread.name,
                committed=True,
            )
        return agent_bin

    def package_for(self, command: str) -> VerifiedNativePackage:
        return self.validate()

    def cursor_scope(self, publication, thread_name, session_id):
        return publication.live_scope(self.wire_root_id, thread_name, session_id)

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
        """Recheck the root, retaining this process's acquired immutable package.

        Creates no root, registry, SQLite database or provider opportunity when
        the owner-only directory or the certified root is absent.
        """
        _private_session_dir(self.validated_root)
        with WireLog(self.validated_root / "bus.jsonl").certified_read() as source:
            marker = source.marker
        if marker.root_id != self.wire_root_id:
            raise IdentityConflict("private initial wire root changed")
        return self.native_package
