"""The process root selected explicitly or from the published route, and its child boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec, PathText, boundary_decoder


class UnstartedLaunch:
    """A process with no selected root; startup must run before use."""


@dataclass(frozen=True)
class LaunchRoot:
    """A process's selected root travels in its arguments, never its environment."""

    path: Annotated[Path, PathText]
    argument: ClassVar[str] = "--comms-root"
    environment_name: ClassVar[str] = "AGENT_COMMS_ROOT"
    _current: ClassVar[LaunchRoot | UnstartedLaunch] = UnstartedLaunch()

    @classmethod
    def from_path(cls, path: str | Path) -> LaunchRoot:
        """Capture cwd/home now; keep symlinks for private-root validation."""
        return cls(Path(path).expanduser().absolute())

    @classmethod
    def unstarted(cls) -> UnstartedLaunch:
        return UnstartedLaunch()

    @classmethod
    @boundary_decoder
    def current(cls) -> LaunchRoot:
        if isinstance(cls._current, UnstartedLaunch):
            # Import here: the route owner also imports LaunchRoot for explicit selection.
            from .active_route import read_active_route

            route = read_active_route()
            if route is None:
                raise RelationViolationError(
                    "No active Comms route is published; --comms-root or --root is required."
                )
            return cls.from_path(route.root)
        return cls._current

    def install(self) -> None:
        LaunchRoot._current = self

    @contextmanager
    def selected(self) -> Iterator[LaunchRoot]:
        """Borrow a selection without changing later callers."""
        previous = LaunchRoot._current
        self.install()
        try:
            yield self
        finally:
            LaunchRoot._current = previous

    def arguments(self) -> tuple[str, str]:
        return self.argument, str(self.path)

    @classmethod
    def startup(cls, arguments: list[str]) -> None:
        if cls.argument in arguments:
            index = arguments.index(cls.argument)
            if index + 1 == len(arguments):
                raise ValueError(f"{cls.argument} requires a root")
            record = FieldCodec.decode(cls, {"path": arguments[index + 1]})
            cls.from_path(record.path).install()
            del arguments[index : index + 2]

    @classmethod
    def environment(cls, source: Mapping[str, str]) -> dict[str, str]:
        """Construct child inheritance without an ambient root override."""
        return {name: value for name, value in source.items() if name != cls.environment_name}
