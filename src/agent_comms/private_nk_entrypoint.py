"""Explicit, pinned private N/K owner launch configuration.

The public ACP attachment never becomes a model executor. A separately
launched owner worker may opt in only with both exact root ID and reviewed
native package. This module installs no marker, participant, schema or input.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_errors import PublicationActivationBlocked

if TYPE_CHECKING:
    from .selected_tool_broker import SelectedToolIntent

ROOT_ID_ENV = "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"
PACKAGE_ENV = "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE"


@dataclass(frozen=True, slots=True)
class PrivateNkLaunch:
    validated_root: Path
    wire_root_id: str
    native_package: Path
    selected_tool_intent: SelectedToolIntent | None

    def validate(self) -> None:
        """Recheck this authority before a new owner process can be reserved."""
        from .cohort_foreground import _preflight

        _preflight(self.validated_root, self.wire_root_id, self.native_package, True)

    def apply_environment(self, environment: dict[str, str]) -> None:
        """Derive the child handoff solely from this retained launch authority."""
        environment.update({
            "AGENT_COMMS_ROOT": str(self.validated_root),
            ROOT_ID_ENV: self.wire_root_id,
            PACKAGE_ENV: str(self.native_package),
        })


def private_nk_launch(root: Path, environment: Mapping[str, str]) -> PrivateNkLaunch | None:
    """Validate *before* constructing a Comms wire or attaching an ACP owner.

    Ordinary/public workers remain unchanged when neither option exists.
    No implicit discovery from a private bus marker can turn a public worker
    into a selected executor. The real runner rechecks the same marker and
    pinned compiled package again before any input reservation/send.
    """
    root_id = environment.get(ROOT_ID_ENV)
    package = environment.get(PACKAGE_ENV)
    if root_id is None and package is None:
        return None
    if (
        type(root_id) is not str
        or len(root_id) != 32
        or any(ch not in "0123456789abcdef" for ch in root_id)
        or type(package) is not str
        or not package
        or not Path(package).is_absolute()
    ):
        raise PublicationActivationBlocked(
            "private N/K owner requires exact root ID and absolute reviewed package"
        )
    if environment.get("PI_PROMPT"):
        raise PublicationActivationBlocked(
            "private N/K owner cannot start with an unbound prompt"
        )
    validated_root = Path(root).expanduser().absolute()  # capture cwd once
    native_package = Path(package)
    # Normal production FULL turns select their coding tools in the runtime.
    # Claim support is not a request for the optional single-write proof mode.
    launch = PrivateNkLaunch(validated_root, root_id, native_package, None)
    launch.validate()
    return launch


def private_nk_from_environment() -> PrivateNkLaunch | None:
    """Use explicit process settings, or the owner-installed active route."""
    environment = dict(os.environ)
    if "AGENT_COMMS_ROOT" not in environment:
        from .active_route import read_active_route

        active_route = read_active_route()
        if active_route is not None:
            environment["AGENT_COMMS_ROOT"] = str(active_route.root)
            environment.setdefault(ROOT_ID_ENV, active_route.wire_root_id)
            environment.setdefault(PACKAGE_ENV, str(active_route.native_package))
    root = Path(environment.get("AGENT_COMMS_ROOT", "~/.agent-comms")).expanduser().absolute()
    return private_nk_launch(root, environment)
