"""Pinned private N/K owner launch configuration.

The public ACP attachment never becomes a model executor. An owner runs only
with an exact root ID and a reviewed native package, both derived from their
owners for each process. This module installs no marker, participant, schema or input.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked

from .native_package import NativePackageError, VerifiedNativePackage, pinned_native_package
from .native_pi import _private_session_dir
from .wire_log import WireLog

@dataclass(frozen=True, slots=True)
class PrivateNkLaunch:
    validated_root: Path
    wire_root_id: str
    native_package: VerifiedNativePackage

    @classmethod
    def derive(cls, root: Path | str | None = None) -> PrivateNkLaunch | None:
        """This root's launch from the owners of its facts, or None for an ordinary root.

        Without a root, the process's selected route names it. The marker owns
        the root ID and this installation's pin owns the package. A parent and
        its child read the same marker and pin, so they derive the same launch.
        """
        from .active_route import resolve_comms_route

        root = resolve_comms_route(root).root
        metadata = WireLog(root / "bus.jsonl").read_metadata_unlocked()
        if not metadata.private:
            return None
        return cls.installed(root, metadata.wire_root_id)

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
