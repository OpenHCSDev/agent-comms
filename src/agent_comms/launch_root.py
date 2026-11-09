"""The process root chosen at startup and handed to child processes."""

from __future__ import annotations

import atexit
import gc
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Self

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import FieldCodec, PathText


def started() -> None:
    """A process has imported its program and is about to work. Freeze that heap now, so every
    full collection while it runs skips the import graph (about 150-300 ms each), and freeze
    again at exit, so shutdown's full collection skips what it built since: that scan cost
    1.3-1.5 s per connector stop, 0.57 s per agent-comms-team call and 3.43 s in Toad's render
    workers. The exit freeze first collects the young generations, so recently dropped cycles
    still finalize (an unclosed writer still flushes). Each process's entry calls it once.
    LaunchRoot.startup never does: CliInvocation runs it per call, and the installer's interpreter
    worker invokes the CLI once per probe in one long-lived process."""
    gc.freeze()
    atexit.register(_skip_final_collection)


def _skip_final_collection() -> None:
    gc.collect(1)
    gc.freeze()


class LaunchSelection(ABC):
    """The current selection owns its scoped installation and root requirement."""

    @abstractmethod
    def require(self) -> LaunchRoot: ...

    def install(self) -> None:
        LaunchRoot._current = self

    @contextmanager
    def selected(self) -> Iterator[Self]:
        previous = LaunchRoot._current
        self.install()
        try:
            yield self
        finally:
            previous.install()


class LaunchRequest(ABC):
    """An explicit root or a request to use the command's published root."""

    @abstractmethod
    def resolve(self, published: Callable[[], LaunchRoot]) -> LaunchRoot: ...


class DefaultLaunch(LaunchRequest):
    def resolve(self, published: Callable[[], LaunchRoot]) -> LaunchRoot:
        return published()


class UnstartedLaunch(LaunchSelection):
    """A process with no selected root; startup must run before use."""

    def require(self) -> LaunchRoot:
        raise RelationViolationError("Comms process did not select a root at startup.")


@dataclass(frozen=True)
class LaunchRoot(LaunchSelection, LaunchRequest):
    """A process's selected root travels in its arguments, never its environment."""

    path: Annotated[Path, PathText]
    argument: ClassVar[str] = "--comms-root"
    environment_name: ClassVar[str] = "AGENT_COMMS_ROOT"
    _current: ClassVar[LaunchSelection] = UnstartedLaunch()

    @classmethod
    def from_path(cls, path: str | Path) -> LaunchRoot:
        """Capture cwd/home now; keep symlinks for private-root validation."""
        root = Path(path).expanduser().absolute()
        # A placeholder that never expanded is a template, not a destination: taking it
        # literally initialises a whole bus inside a brace-named directory (2026-11-05,
        # a checkout carried {root}/bus.sqlite3 until a checkpoint adopted it).
        if any(marker in str(root) for marker in "{}"):
            raise ValueError(f"A root path still holding a placeholder is not a root: {path}")
        return cls(root)

    @classmethod
    def unstarted(cls) -> UnstartedLaunch:
        return UnstartedLaunch()

    @classmethod
    def current(cls) -> LaunchRoot:
        return cls._current.require()

    def require(self) -> LaunchRoot:
        return self

    def resolve(self, published: Callable[[], LaunchRoot]) -> LaunchRoot:
        return self

    def arguments(self) -> tuple[str, str]:
        return self.argument, str(self.path)

    @classmethod
    def startup(cls, arguments: list[str], published: Callable[[], LaunchRoot]) -> LaunchRoot:
        """Select this process's root from its own arguments, never from a child's.

        A `--` ends this process's options: everything after it is a command this
        process will run, so its `--comms-root` names that child's root. Reading it
        here selected the child's root for this process, which then failed with an
        unrelated thread-registration error instead of naming the real mistake.
        """
        own = arguments[:arguments.index("--")] if "--" in arguments else arguments
        if cls.argument in own:
            index = arguments.index(cls.argument)
            if index + 1 == len(arguments):
                raise ValueError(f"{cls.argument} requires a root")
            record = FieldCodec.decode(cls, {"path": arguments[index + 1]})
            root = cls.from_path(record.path)
            del arguments[index : index + 2]
        else:
            root = published()
        root.install()
        return root

    @classmethod
    def environment(cls, source: Mapping[str, str]) -> dict[str, str]:
        """Construct child inheritance without an ambient root override."""
        return {name: value for name, value in source.items() if name != cls.environment_name}
