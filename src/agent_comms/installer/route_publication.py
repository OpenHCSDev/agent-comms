"""Validated publication of the default active comms route."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_comms.execution.private_nk_entrypoint import RootLaunch
from agent_comms.installer.active_route import ActiveRoute, ActiveRouteFile, RouteFile
from agent_comms.storage.private_path import OwnedDirectoryRole


class ActiveRoutePublisher(ActiveRouteFile):
    """Route file writer that verifies the target root's private native launch before publishing."""

    __slots__ = ()

    def publish[Result](
        self,
        route: ActiveRoute,
        change: Callable[[RouteFile, Callable[[], None], int], Result],
    ) -> Result:
        """Stage and validate before EX; publication uses the current file's policy."""
        if self.path.name != RouteFile.filename or not self.path.is_absolute():
            raise ValueError("active comms route requires its absolute route path")
        payload = route.encoded()
        RootLaunch.derive(route).require_private().validate()
        with self._directory() as directory:
            @contextmanager
            def prepare(_: None) -> Iterator[tuple[RouteFile, Callable[[], None]]]:
                original = RouteFile.read(self.path)
                with original.writer(self.path, directory).prepared(payload) as publish:
                    RootLaunch.derive(route).require_private().validate()
                    yield original, publish

            def commit(prepared: tuple[RouteFile, Callable[[], None]]) -> Result:
                original, publish = prepared
                OwnedDirectoryRole.held(
                    directory,
                    self.path.parent,
                    ValueError("active comms route directory changed"),
                )
                route.observe_root()
                return change(original, publish, directory)

            return self.change_current(lambda: None, prepare, commit)


def route_new_bus(route: ActiveRoute, path: Path) -> str:
    """Route to this bus unless the file already selects one: install's answer and the
    owner's repair. A replaced file is kept beside the new one under a backup name, so a
    live route is still only ever installed once."""
    return ActiveRoutePublisher(path).publish(
        route,
        lambda current, publish, directory: current.route_new_bus(route, path, publish, directory),
    )
