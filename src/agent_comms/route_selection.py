"""Captured route and root policy; writes use the policy's own lifetime."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .active_route import CommsRoute, RouteChanged
from .launch_runtime import LaunchRecord, LaunchRoot


def current_root() -> Path:
    return LaunchRecord.current().root_selection.route().observe_root()


def root_is_current(root: str | Path) -> bool:
    return current_root() == Path(root).expanduser().resolve()


@dataclass(frozen=True)
class RouteSelection:
    route: CommsRoute
    root_selection: LaunchRoot

    @classmethod
    def capture(cls) -> RouteSelection:
        return cls.for_child(LaunchRecord.current())

    @classmethod
    def capture_for(cls, source: str | Path) -> RouteSelection:
        selection = cls.capture()
        if Path(source).expanduser().resolve() != selection.root_selection.path:
            raise RouteChanged("Comms route changed; reopen this view")
        return selection

    @classmethod
    def for_child(cls, record: LaunchRecord) -> RouteSelection:
        route = record.root_selection.route()
        if route.observe_root() != record.root_selection.path:
            raise RouteChanged("ACP route changed while selecting its child")
        return cls(route, record.root_selection)
