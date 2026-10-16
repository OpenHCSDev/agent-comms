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

from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked

from . import launch_runtime
from .native_pi import _private_session_dir, _trusted_package
from .wire_metadata import WireRootIdText

if TYPE_CHECKING:
    from .selected_tool_broker import SelectedToolIntent


@dataclass(frozen=True, slots=True)
class PrivateNkLaunch:
    validated_root: Path
    wire_root_id: str
    native_package: Path
    selected_tool_intent: SelectedToolIntent | None

    @classmethod
    def from_environment(cls, root: Path, environment: Mapping[str, str]) -> PrivateNkLaunch | None:
        """Decode the explicit launch selection without performing an admission.

        Observing a thread or publishing activity does not launch a native owner.
        Actual worker/ACP entrypoints and process handoffs call validate before
        admission; retaining this selection cannot grant that authority.
        """
        root_id = environment.get(launch_runtime.ROOT_ID_ENV)
        package = environment.get(launch_runtime.PACKAGE_ENV)
        if root_id is None and package is None:
            return None
        from .active_route import AbsoluteRoutePathText

        try:
            root_id = WireRootIdText.decode(root_id)
            native_package = AbsoluteRoutePathText.decode(package)
        except (TypeError, ValueError) as error:
            raise PublicationActivationBlocked(
                "private N/K owner requires exact root ID and absolute reviewed package"
            ) from error
        if environment.get("PI_PROMPT"):
            raise PublicationActivationBlocked(
                "private N/K owner cannot start with an unbound prompt"
            )
        validated_root = Path(root).expanduser().absolute()  # capture cwd once
        # Normal production FULL turns select their coding tools in the runtime.
        # Claim support is not a request for the optional single-write proof mode.
        return cls(validated_root, root_id, native_package, None)

    @classmethod
    def current(cls) -> PrivateNkLaunch | None:
        """Decode the original explicit environment or owner-installed route."""
        environment = dict(os.environ)
        if "AGENT_COMMS_ROOT" not in environment:
            from .active_route import read_active_route

            active_route = read_active_route()
            if active_route is not None:
                environment["AGENT_COMMS_ROOT"] = str(active_route.root)
                environment.setdefault(launch_runtime.ROOT_ID_ENV, active_route.wire_root_id)
                environment.setdefault(launch_runtime.PACKAGE_ENV, str(active_route.native_package))
        root = Path(environment.get("AGENT_COMMS_ROOT", "~/.agent-comms")).expanduser().absolute()
        return cls.from_environment(root, environment)

    def validate(self) -> Path:
        """Recheck this authority before a new owner process can be reserved.

        Creates no root, registry, SQLite database or provider opportunity when
        the owner-only directory or the reviewed copied Pi is absent.
        """
        from .comms import Comms
        from .message_bus import MessageBus

        _private_session_dir(self.validated_root)
        cli = _trusted_package(self.native_package)
        comms = Comms(self.validated_root)
        bus = MessageBus(
            self.validated_root / "bus.jsonl", comms.registry, private_response_writes=True
        )
        with bus.log.certified_read() as source:
            marker = source.marker
        if marker.root_id != self.wire_root_id:
            raise IdentityConflict("private initial wire root changed")
        return cli

    def apply_environment(self, environment: dict[str, str]) -> None:
        """Derive the child handoff solely from this retained launch authority."""
        environment.update({
            "AGENT_COMMS_ROOT": str(self.validated_root),
            launch_runtime.ROOT_ID_ENV: self.wire_root_id,
            launch_runtime.PACKAGE_ENV: str(self.native_package),
        })


def private_nk_from_environment() -> PrivateNkLaunch | None:
    """Use explicit process settings, or the owner-installed active route."""
    launch = PrivateNkLaunch.current()
    if launch is not None:
        launch.validate()
    return launch
