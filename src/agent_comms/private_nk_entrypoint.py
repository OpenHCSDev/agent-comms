"""Pinned private N/K owner launch configuration.

The public ACP attachment never becomes a model executor. An owner runs only
with an exact root ID and a reviewed native package, both derived from their
owners for each process. This module installs no marker, participant, schema or input.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked

from .native_package import NativePackageError, pinned_native_package
from .native_pi import _private_session_dir, _trusted_package
from .wire_log import WireLog

@dataclass(frozen=True, slots=True)
class PrivateNkLaunch:
    validated_root: Path
    wire_root_id: str
    native_package: Path

    @classmethod
    def derive(cls, root: Path) -> PrivateNkLaunch | None:
        """This root's launch from the owners of its facts, or None for an ordinary root.

        The marker owns the root ID. A published route for this root owns its
        package; otherwise this installation's pin does. A parent and its child
        read the same marker, route and pin, so they derive the same launch.
        """
        metadata = WireLog(root / "bus.jsonl").read_metadata_unlocked()
        if not metadata.private:
            return None
        from .active_route import read_active_route

        route = read_active_route()
        if route is not None and route.root == root:
            return cls(root, route.wire_root_id, route.native_package)
        try:
            package = pinned_native_package()
        except NativePackageError as error:
            raise PublicationActivationBlocked(
                "private owner launch needs this installation's prepared native package"
            ) from error
        return cls(root, metadata.wire_root_id, package)

    def validate(self) -> Path:
        """Recheck this authority before a new owner process can be reserved.

        Creates no root, registry, SQLite database or provider opportunity when
        the owner-only directory or the reviewed copied Pi is absent.
        """
        _private_session_dir(self.validated_root)
        cli = _trusted_package(self.native_package)
        with WireLog(self.validated_root / "bus.jsonl").certified_read() as source:
            marker = source.marker
        if marker.root_id != self.wire_root_id:
            raise IdentityConflict("private initial wire root changed")
        return cli
