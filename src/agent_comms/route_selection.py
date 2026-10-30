"""A client's selection of the current Comms route.

The route owner (:mod:`agent_comms.active_route`) validates the published
selection and private marker without constructing a service. Writes need no
route lock: each store refuses a write under maintenance inside its own write
transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_comms import errors

from .active_route import CommsRoute, resolve_comms_route
from .bus import Bus
from .launch_root import LaunchRoot
from .recorded_bus import RecordedBusIdentity


@dataclass(frozen=True)
class RouteSelection:
    route: CommsRoute
    root: Path
    bus_identity: RecordedBusIdentity

    @classmethod
    def of(cls, root: LaunchRoot) -> RouteSelection:
        """Resolve the root held by this client, independent of the process default."""
        route = resolve_comms_route(root.path)
        selected_root = route.observe_root()
        return cls(
            route, selected_root, RecordedBusIdentity.capture(Bus.path_in(selected_root))
        )

    @classmethod
    def capture(cls) -> RouteSelection:
        """Capture the process selection where no client root is held yet."""
        return cls.of(LaunchRoot.current())

    def root_is_current(self, source: str | Path) -> bool:
        """Whether a source belongs to this selection, not the process default."""
        return Path(source).expanduser().resolve() == self.root

    def require_source(self, source: str | Path) -> RouteSelection:
        """Refuse a foreign source before using this client's selection."""
        if not self.root_is_current(source):
            raise errors.RouteChangedError(errors.RouteChangedError.reader_sentence)
        return self

    @classmethod
    def for_child(cls, env: Mapping[str, str], cwd: str | Path) -> RouteSelection:
        """Capture the explicit process selection before launching its child."""
        return cls.of(LaunchRoot.current())
