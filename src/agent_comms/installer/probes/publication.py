from __future__ import annotations

from agent_comms.active_route import RouteFile, RouteFileSelection, active_route_path
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe


class PublicationProbe(RuntimeProbe[None]):
    """The selected interpreter publishes itself: console launchers and harness links move
    to it, then ``current`` does."""

    def run(self) -> None:
        # Imported here: publication -> runtime_resolution -> interpreter_operation is a cycle.
        from agent_comms.installer.publication import RuntimePublication

        RuntimePublication.installed().publish_links()


class SelectedRouteProbe(TargetProbe, RuntimeProbe[RouteFileSelection]):
    """The staged runtime reads the published route before source changes."""

    def run(self) -> RouteFileSelection:
        return RouteFile.read(active_route_path()).selection()
