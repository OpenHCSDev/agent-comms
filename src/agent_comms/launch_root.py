"""The process root selected explicitly or from the published route, and its child boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .field_codec import FieldCodec, PathText

if TYPE_CHECKING:
    from .active_route import CommsRoute


class UnstartedLaunch:
    """A process with no explicit root uses the published route."""

    def root(self) -> LaunchRoot:
        return LaunchRoot.from_path(self.route().root)

    def route(self) -> CommsRoute:
        # The route module imports LaunchRoot; load it after both states exist.
        from .active_route import ActiveRoute

        return ActiveRoute.published()


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
    def current(cls) -> LaunchRoot:
        return cls._current.root()

    @classmethod
    def current_route(cls) -> CommsRoute:
        return cls._current.route()

    def root(self) -> LaunchRoot:
        return self

    def route(self) -> CommsRoute:
        from .active_route import LocalRoute

        return LocalRoute(self.path)

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
