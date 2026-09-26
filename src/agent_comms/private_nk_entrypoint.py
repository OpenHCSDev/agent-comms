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

from .cohort_foreground import _preflight
from .coordination_store import PublicationActivationBlocked

ROOT_ID_ENV = "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"
PACKAGE_ENV = "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE"


@dataclass(frozen=True, slots=True)
class PrivateNkLaunch:
    wire_root_id: str
    native_package: Path


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
            "private N/K owner cannot start with an unbound legacy prompt"
        )
    native_package = Path(package)
    _preflight(Path(root).absolute(), root_id, native_package, True)
    return PrivateNkLaunch(root_id, native_package)


def private_nk_from_environment() -> PrivateNkLaunch | None:
    """Use the same explicit root/environment preflight for ACP and worker."""
    root = Path(os.environ.get("AGENT_COMMS_ROOT", "~/.agent-comms")).expanduser().absolute()
    return private_nk_launch(root, os.environ)
