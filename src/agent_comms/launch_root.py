"""The explicit process root and its child-process boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec, PathText


@dataclass(frozen=True)
class LaunchRoot:
    """A process's selected root travels in its arguments, never its environment."""

    path: Annotated[Path, PathText]
    argument: ClassVar[str] = "--comms-root"
    environment_name: ClassVar[str] = "AGENT_COMMS_ROOT"
    _current: ClassVar[LaunchRoot | None] = None

    def __post_init__(self) -> None:
        # Capture cwd/home now; keep symlinks for private-root validation.
        object.__setattr__(self, "path", self.path.expanduser().absolute())

    @classmethod
    def current(cls) -> LaunchRoot:
        if cls._current is None:
            raise RelationViolationError(
                "Comms requires an explicit launch root (--comms-root or --root)."
            )
        return cls._current

    def install(self) -> None:
        LaunchRoot._current = self

    @contextmanager
    def selected(self) -> Iterator[None]:
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
            record.install()
            del arguments[index:index + 2]

    @classmethod
    def environment(cls, source: Mapping[str, str]) -> dict[str, str]:
        """Construct child inheritance without an ambient root override."""
        return {name: value for name, value in source.items() if name != cls.environment_name}
