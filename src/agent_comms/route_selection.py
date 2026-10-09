"""A client's selection of the current Comms route and its guarded writes.

The route owner (:mod:`agent_comms.active_route`) validates the published
selection and private marker without constructing a service. A client captures
one selection, then enters :func:`selected_write` in the worker that performs
the side effect, so a route publication cannot interleave with it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .active_route import CommsRoute, guard_default_route_write, resolve_comms_route

T = TypeVar("T")


def current_root() -> Path:
    """Resolve the explicit override or the validated default Comms route."""
    return resolve_comms_route().observe_root()


def root_is_current(root: str | Path) -> bool:
    """Fail closed if a client's root is no longer the current route."""
    try:
        return current_root() == Path(root).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return False


def implicit_root() -> bool:
    """Whether this process's requests select the managed default route."""
    return "AGENT_COMMS_ROOT" not in os.environ


def child_root(env: Mapping[str, str], cwd: str | Path) -> Path:
    """Resolve a child's wire; never mistake the parent's route for another HOME."""
    if "AGENT_COMMS_ROOT" not in env:
        home_key = "USERPROFILE" if os.name == "nt" else "HOME"
        home = env.get(home_key)
        if (not home or not Path(home).is_absolute()
                or home != str(Path.home())
                or "AGENT_COMMS_ROOT" in os.environ):
            raise ValueError(
                "ACP default route requires the client process HOME and no parent "
                "root override; set an explicit child AGENT_COMMS_ROOT"
            )
        return current_root()
    text = env["AGENT_COMMS_ROOT"]
    if text == "~" or text.startswith("~/"):
        home = env.get("HOME") if os.name != "nt" else env.get("USERPROFILE")
        if not home or not Path(home).is_absolute():
            raise ValueError("ACP home for maintenance admission is unknown")
        text = str(Path(home) / text[2:]) if text != "~" else home
    elif text.startswith("~"):
        raise ValueError("Unsupported ACP home expansion during maintenance admission")
    raw = Path(text)
    return (raw if raw.is_absolute() else Path(cwd) / raw).resolve()


@dataclass(frozen=True)
class RouteSelection:
    route: CommsRoute
    root: Path
    implicit: bool

    @classmethod
    def capture(cls, source: str | Path | None = None) -> RouteSelection:
        route = resolve_comms_route()
        selection = cls(route, route.observe_root(), implicit_root())
        if source is not None and Path(source).expanduser().resolve() != selection.root:
            raise ValueError("Comms route changed; reopen this view")
        return selection

    @classmethod
    def for_child(cls, env: Mapping[str, str], cwd: str | Path) -> RouteSelection:
        """Capture selection before projecting a root into a child environment."""
        root = child_root(env, cwd)
        implicit = "AGENT_COMMS_ROOT" not in env
        route = resolve_comms_route() if implicit else resolve_comms_route(root)
        if route.observe_root() != root:
            raise ValueError("ACP route changed while selecting its child")
        return cls(route, root, implicit)


@contextmanager
def selected_write(root: str | Path, *, implicit: bool) -> Iterator[None]:
    """Guard one default-root side effect through the actual synchronous sink.

    Explicit overrides retain their independent-root behavior.
    """
    scope = guard_default_route_write(Path(root)) if implicit else nullcontext()
    with scope:
        if implicit and not root_is_current(root):
            raise ValueError("default Comms route changed before write")
        yield


def run_selected_write(
    root: str | Path,
    operation: Callable[..., T],
    *args: object,
    implicit: bool,
    **kwargs: object,
) -> T:
    """Enter route admission inside the worker that actually performs the write."""
    with selected_write(root, implicit=implicit):
        return operation(*args, **kwargs)
