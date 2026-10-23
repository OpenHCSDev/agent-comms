"""The registry owns whether a saved transcript follows its bus or its harness."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, TYPE_CHECKING

from .active_route import CommsRoute
from .errors import RelationViolationError
from .field_codec import NullMember, PathText, RecordMember, SubclassUnion, WireTransient
from .unstarted_runtime import UnstartedRuntime

if TYPE_CHECKING:
    from .agent_backend import AgentBackend, RuntimeLocation


class SessionFile(SubclassUnion, affix="SessionFile"):
    """A registry transcript location, or no saved transcript yet."""

    @abstractmethod
    def bound_to(self, root: Path) -> SessionFile:
        """The location read from the registry under ``root``."""

    @abstractmethod
    def location(self, backend: type[AgentBackend]) -> RuntimeLocation: ...

    @abstractmethod
    def writable_location(self, backend: type[AgentBackend]) -> RuntimeLocation: ...

    @abstractmethod
    def applied(self, previous: SessionFile) -> SessionFile: ...


@dataclass(frozen=True, slots=True)
class RegistrySessionLocation(RecordMember, SessionFile, declared_name=None):
    """A saved transcript whose storage owner was selected at publication."""

    path: Annotated[Path, PathText]

    @classmethod
    def from_path(cls, path: Path | str, root: Path) -> BoundSessionLocation:
        absolute = Path(path)
        if not absolute.is_absolute():
            raise ValueError(f"Saved session publication requires an absolute path: {path}")
        absolute = absolute.resolve()
        root = root.resolve()
        if absolute.is_relative_to(root):
            return BusSessionLocation(absolute.relative_to(root)).bound_to(root)
        return ExternalSessionLocation(absolute).bound_to(root)

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        raise RelationViolationError(f"Saved session {self.path} must be bound by its registry")

    def writable_location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return self.location(backend)

    def applied(self, previous: SessionFile) -> SessionFile:
        return self

    @abstractmethod
    def resolve(self, root: Path) -> Path: ...

    def writable(self, root: Path) -> Path:
        return self.resolve(root)


@dataclass(frozen=True)
class BoundSessionLocation:
    """A wire location bound to the root whose registry decoded it."""

    root: Annotated[Path, WireTransient()] = field(repr=False, compare=False)

    def bound_to(self, root: Path) -> BoundSessionLocation:
        """Publishing into another root keeps the transcript this location names."""
        root = root.resolve()
        if self.root == root:
            return self
        return RegistrySessionLocation.from_path(self.resolve(self.root), root)

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return backend.location_type(str(self.resolve(self.root)))

    def writable_location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return backend.location_type(str(self.writable(self.root)))


@dataclass(frozen=True, slots=True)
class BusSessionLocation(RegistrySessionLocation):
    """A transcript stored under the bus root; a copied root reads its own copy."""

    def __post_init__(self):
        if self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError(f"Bus session must be relative to its root: {self.path}")

    def bound_to(self, root: Path) -> BoundBusSession:
        return BoundBusSession(self.path, root.resolve())

    def resolve(self, root: Path) -> Path:
        root = root.resolve()
        path = (root / self.path).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Bus session escapes its root: {self.path}")
        return path


@dataclass(frozen=True, slots=True)
class ExternalSessionLocation(RegistrySessionLocation):
    """A harness transcript outside every bus root; only the published root writes it."""

    def __post_init__(self):
        if not self.path.is_absolute():
            raise ValueError(f"External session must be absolute: {self.path}")

    def bound_to(self, root: Path) -> BoundExternalSession:
        return BoundExternalSession(self.path, root.resolve())

    def resolve(self, root: Path) -> Path:
        return self.path

    def writable(self, root: Path) -> Path:
        if root.resolve() != CommsRoute.current().observe_root():
            raise RelationViolationError(
                f"Copied bus {root} cannot write external session {self.path}")
        return self.resolve(root)


@dataclass(frozen=True)
class BoundBusSession(BoundSessionLocation, BusSessionLocation, declared_name=None):
    pass


@dataclass(frozen=True)
class BoundExternalSession(BoundSessionLocation, ExternalSessionLocation, declared_name=None):
    pass


@dataclass(frozen=True, slots=True)
class NoSessionFile(NullMember, SessionFile):
    def bound_to(self, root: Path) -> NoSessionFile:
        return self

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return UnstartedRuntime(backend)

    def writable_location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return self.location(backend)

    def applied(self, previous: SessionFile) -> SessionFile:
        return previous
