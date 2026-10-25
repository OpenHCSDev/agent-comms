"""Pinned private N/K owner launch configuration.

The public ACP attachment never becomes a model executor. An owner runs only
with an exact root ID and a reviewed native package, both derived from their
owners for each process. This module installs no marker, participant, schema or input.
"""

from __future__ import annotations

from agent_comms.field_codec import RootId

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked

from .bus import Bus
from .locked_store import RegistryFile

from .native_arguments import NativeArguments
from .native_package import (
    NativePackageError,
    VerifiedNativePackage,
    pinned_native_package,
)
from agent_comms.native_session_files import NativeSessionFiles

if TYPE_CHECKING:
    from .active_route import CommsRoute
    from .launch_runtime import LaunchRuntime
    from .threads import Thread


class RootLaunch(ABC):
    """Native launch policy derived from the registry's existing authority."""

    __slots__ = ()

    @staticmethod
    def derive(route: CommsRoute) -> RootLaunch:
        """This route's launch from the owners of its facts.

        The registry guard owns the native namespace; the installed pin owns
        the package. No bus-file metadata participates in either decision.
        """
        from .private_registry_guard import RegistryPrivacy

        root = route.observe_root()
        return RegistryPrivacy.capture(root).launch(root)

    @property
    @abstractmethod
    def child_root(self) -> Path:
        """The root a launched child receives in its launch arguments."""

    @abstractmethod
    def require_private(self) -> PrivateNkLaunch:
        """This root's private launch; a shared root refuses."""

    @abstractmethod
    def check(self) -> None:
        """Refuse a launch whose root was replaced; a shared root has nothing to check."""

    def restart_entrypoint(self, source_binary: str, runtime: LaunchRuntime) -> str:
        """Every prepared owner runs the selected runtime's pinned entrypoint."""
        self.check()
        return runtime.native_entrypoint

    def prepare_owner(self, thread: Thread, agent_bin: str, runtime: LaunchRuntime) -> str:
        """Explicit owner launch prepares registry authority, then native tables."""
        from .registration import Registration

        if agent_bin == NativeArguments.default_binary:
            agent_bin = runtime.native_entrypoint
        else:
            agent_bin = str(Path(shutil.which(agent_bin) or agent_bin).resolve(strict=True))
        root = self.child_root
        Registration(RegistryFile.path_in(root)).prepare_native_root()
        return agent_bin

    @abstractmethod
    def launched_package(self) -> VerifiedNativePackage:
        """The reviewed package every native execution from this root runs. Which commands
        may start it is NativePiRpcLaunch._command_selection's, for every root alike."""


@dataclass(frozen=True, slots=True)
class SharedRootLaunch(RootLaunch):
    """An unprepared registry publishes no native cursor before an explicit owner launch."""

    root: Path

    @property
    def child_root(self) -> Path:
        """An ordinary root keeps its canonical child path across symlink changes."""
        return self.root.resolve()

    def require_private(self) -> PrivateNkLaunch:
        raise PublicationActivationBlocked("requires a private bus root")

    def check(self) -> None:
        pass

    def launched_package(self) -> VerifiedNativePackage:
        return VerifiedNativePackage.acquire(pinned_native_package())


@dataclass(frozen=True, slots=True)
class PrivateNkLaunch(RootLaunch):
    validated_root: Path
    wire_root_id: RootId
    native_package: VerifiedNativePackage

    @property
    def child_root(self) -> Path:
        """A private child derives the same launch from its validated root."""
        return self.validated_root

    def require_private(self) -> PrivateNkLaunch:
        return self

    def check(self) -> None:
        self.validate()

    def launched_package(self) -> VerifiedNativePackage:
        return self.validate()

    @classmethod
    def installed(cls, root: Path, wire_root_id: RootId) -> PrivateNkLaunch:
        """A private root's launch with this installation's pinned native package."""
        try:
            package = VerifiedNativePackage.acquire(pinned_native_package())
        except NativePackageError as error:
            raise PublicationActivationBlocked(
                f"private owner launch needs this installation's prepared native package: {error}"
            ) from error
        return cls(root, wire_root_id, package)

    def validate(self) -> VerifiedNativePackage:
        """Recheck the bus root while keeping this process's chosen native package.

        If the private directory or checked bus root is missing, create no
        directory, registry or database and make no provider request.
        """
        NativeSessionFiles.require_private_directory(self.validated_root)
        from .registry_store import RegistryStore

        Bus(Bus.path_in(self.validated_root)).revision()
        registry = RegistryStore(RegistryFile.path_in(self.validated_root))
        with registry.guarded_read():
            registry.private_guard_unlocked().require_root(
                self.wire_root_id, IdentityConflict("Native registry namespace changed"),
            )
        return self.native_package
