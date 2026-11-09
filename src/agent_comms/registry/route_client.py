"""One client call attached to a selected route: the runtime check, the root and its registry.

Comms builds every store on top of this; a caller that needs one store inherits only the
attachment, so a per-tool-call hook never imports or builds the whole facade.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from agent_comms.agent_events import RuntimeInfoObservation
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.root_runtime import RootRuntime
from agent_comms.execution.runtime_info_source import RuntimeInfoSource
from agent_comms.installer.active_route import CommsRoute
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.registration import Registration
from agent_comms.storage.locked_store import RegistryFile


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


class ActivityClient(RouteClient, RuntimeInfoSource[RuntimeInfoObservation]):
    """The one store a turn-phase report writes."""

    agents: AgentActivity

    def __init__(self, route: CommsRoute) -> None:
        super().__init__(route)
        self.agents = self.activity_store()

    def activity_store(self) -> AgentActivity:
        """A turn-phase report reads no inbox, so its store sees no delivery stall."""
        return AgentActivity(self.root, self.registry)
