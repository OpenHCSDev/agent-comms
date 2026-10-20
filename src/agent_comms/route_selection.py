"""A client's selection of the current Comms route and its guarded writes.

The route owner (:mod:`agent_comms.active_route`) validates the published
selection and private marker without constructing a service. A client captures
one selection, then enters :func:`selected_write` in the worker that performs
the side effect, so a route publication cannot interleave with it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .active_route import CommsRoute, guard_default_route_write, resolve_comms_route
from .launch_root import LaunchRoot


class RouteChanged(ValueError):
    """The selected Comms route was replaced; reopen against the current one."""

T = TypeVar("T")


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
    implicit: bool

    @classmethod
    def capture(cls, source: str | Path | None = None) -> RouteSelection:
        route = resolve_comms_route()
        selection = cls(route, route.observe_root(), False)
        if source is not None and Path(source).expanduser().resolve() != selection.root:
            raise RouteChanged("Comms route changed; reopen this view")
        return selection

    @classmethod
    def for_child(cls, env: Mapping[str, str], cwd: str | Path) -> RouteSelection:
        """Capture the explicit process selection before launching its child."""
        root = child_root(env, cwd)
        return cls(resolve_comms_route(root), root, False)


@contextmanager
def selected_write(root: str | Path, *, implicit: bool) -> Iterator[None]:
    """Guard one default-root side effect through the actual synchronous sink.

    Explicit overrides retain their independent-root behavior.
    """
    scope = guard_default_route_write(Path(root)) if implicit else nullcontext()
    with scope:
        if implicit and not root_is_current(root):
            raise RouteChanged("default Comms route changed before write")
        yield


def run_selected_write(
    root: str | Path,
    operation: Callable[..., T],
    *args: object,
    implicit: bool,
    **kwargs: object,
) -> T:
    """Acquire the route locks inside the worker that actually performs the write."""
    with selected_write(root, implicit=implicit):
        return operation(*args, **kwargs)
