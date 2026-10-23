"""The registry owns whether a saved transcript follows its bus or its harness."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TYPE_CHECKING

from .active_route import CommsRoute
from .field_codec import NullMember, PathText, RecordMember, SubclassUnion
from .unstarted_runtime import UnstartedRuntime

if TYPE_CHECKING:
    from .agent_backend import AgentBackend, RuntimeLocation


class SessionFile(SubclassUnion, affix="SessionFile"):
    """A registry transcript location, or no saved transcript yet."""

    @abstractmethod
    def location(self, backend: type[AgentBackend]) -> RuntimeLocation: ...

    @abstractmethod
    def applied(self, previous: SessionFile) -> SessionFile: ...


@dataclass(frozen=True, slots=True)
class RegistrySessionLocation(RecordMember, SessionFile, declared_name=None):
    """A saved transcript whose storage owner was selected at publication."""

    path: Annotated[Path, PathText]

    @classmethod
    def from_path(cls, path: Path | str, root: Path) -> RegistrySessionLocation:
        absolute = Path(path)
        if not absolute.is_absolute():
            raise ValueError(f"Saved session publication requires an absolute path: {path}")
        absolute = absolute.resolve()
        root = root.resolve()
        if absolute.is_relative_to(root):
            return BusSessionLocation(absolute.relative_to(root))
        return ExternalSessionLocation(absolute)

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return backend.location_type(self)

    def applied(self, previous: SessionFile) -> SessionFile:
        return self

    @abstractmethod
    def resolve(self, root: Path) -> Path: ...

    def writable(self, root: Path) -> Path:
        return self.resolve(root)


@dataclass(frozen=True, slots=True)
class BusSessionLocation(RegistrySessionLocation):
    def __post_init__(self):
        if self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError(f"Bus session must be relative to its root: {self.path}")

    def resolve(self, root: Path) -> Path:
        root = root.resolve()
        path = (root / self.path).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Bus session escapes its root: {self.path}")
        return path


@dataclass(frozen=True, slots=True)
class ExternalSessionLocation(RegistrySessionLocation):
    def __post_init__(self):
        if not self.path.is_absolute():
            raise ValueError(f"External session must be absolute: {self.path}")

    def resolve(self, root: Path) -> Path:
        return self.path

    def writable(self, root: Path) -> Path:
        if root.resolve() != CommsRoute.current().observe_root():
            raise ValueError(f"Copied bus {root} cannot write external session {self.path}")
        return self.resolve(root)


@dataclass(frozen=True, slots=True)
class NoSessionFile(NullMember, SessionFile):
    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return UnstartedRuntime(backend)

    def applied(self, previous: SessionFile) -> SessionFile:
        return previous
