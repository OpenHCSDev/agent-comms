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


def current_root() -> Path:
    """Resolve the explicit override or the validated default Comms route."""
    return resolve_comms_route().observe_root()


def root_is_current(root: str | Path) -> bool:
    """Fail closed if a client's root is no longer the current route."""
    return current_root() == Path(root).expanduser().resolve()


def child_root(env: Mapping[str, str], cwd: str | Path) -> Path:
    """The explicit process launch selects its child; HOME never supplies a route."""
    return LaunchRoot.current().path


@dataclass(frozen=True)
class RouteSelection:
    route: CommsRoute
    root: Path
    bus_identity: RecordedBusIdentity

    @classmethod
    def capture(cls, source: str | Path | None = None) -> RouteSelection:
        route = resolve_comms_route()
        root = route.observe_root()
        selection = cls(route, root, RecordedBusIdentity.capture(Bus.path_in(root)))
        if source is not None and Path(source).expanduser().resolve() != selection.root:
            raise errors.RouteChangedError(errors.RouteChangedError.reader_sentence)
        return selection

    @classmethod
    def for_child(cls, env: Mapping[str, str], cwd: str | Path) -> RouteSelection:
        """Capture the explicit process selection before launching its child."""
        root = child_root(env, cwd)
        return cls(
            resolve_comms_route(root), root, RecordedBusIdentity.capture(Bus.path_in(root))
        )
