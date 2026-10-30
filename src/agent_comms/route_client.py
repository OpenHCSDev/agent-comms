"""One client call attached to a selected route: the runtime check, the root and its registry.

Comms builds every store on top of this; a caller that needs one store inherits only the
attachment, so a per-tool-call hook never imports or builds the whole facade.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from .active_route import CommsRoute
from .agent_activity import AgentActivity
from .launch_runtime import LaunchRuntime
from .locked_store import RegistryFile
from .registration import Registration
from .root_runtime import RootRuntime


class RouteClient:
    def __init__(self, route: CommsRoute) -> None:
        self.route = route
        RootRuntime.require_attachable(self.route.root, LaunchRuntime.running())
        self.route.observe_root()
        self.registry = Registration(RegistryFile.path_in(self.root))

    @property
    def root(self) -> Path:
        return self.route.root

    @classmethod
    @contextmanager
    def client(cls, route: CommsRoute, **stores: object) -> Iterator[Self]:
        """One complete client call on this selected route."""
        yield cls(route, **stores)


class ActivityClient(RouteClient):
    """The one store a turn-phase report writes."""

    def __init__(self, route: CommsRoute) -> None:
        super().__init__(route)
        self.agents = AgentActivity(self.root, self.registry)
