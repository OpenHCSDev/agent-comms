"""The registry owns whether a saved transcript follows its bus or its harness."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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


@dataclass(frozen=True, slots=True, eq=False)
class RegistrySessionLocation(RecordMember, SessionFile, declared_name=None):
    """A saved transcript whose storage owner was selected at publication."""

    path: Annotated[Path, PathText]

    def __eq__(self, other: object) -> bool:
        """A bus path is relative and an external one absolute, so the path is the location;
        the root a registry bound it to says where it is read, not which one it is."""
        return isinstance(other, RegistrySessionLocation) and self.path == other.path

    def __hash__(self) -> int:
        return hash(self.path)

    @classmethod
    def from_path(cls, path: Path | str, root: Path) -> BoundSessionLocation:
        absolute = Path(path)
        if not absolute.is_absolute():
            raise ValueError(f"Saved session publication requires an absolute path: {path}")
        root = root.resolve()
        return cls.publish(absolute.resolve(), root, root)

    @staticmethod
    def publish(absolute: Path, root: Path, publisher: Path) -> BoundSessionLocation:
        """A path under the root is the bus's own; any other belongs to its publishing root."""
        if absolute.is_relative_to(root):
            return BusSessionLocation(absolute.relative_to(root)).bound_to(root)
        return ExternalSessionLocation(absolute, publisher).bound_to(root)

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        raise RelationViolationError(f"Saved session {self.path} must be bound by its registry")

    def writable_location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return self.location(backend)

    def applied(self, previous: SessionFile) -> SessionFile:
        return self

    @abstractmethod
    def resolve(self, root: Path) -> Path: ...

    @abstractmethod
    def publisher(self, root: Path) -> Path:
        """The root whose registry published this transcript, read under ``root``."""

    def writable(self, root: Path) -> Path:
        return self.resolve(root)


@dataclass(frozen=True, eq=False)
class BoundSessionLocation(RegistrySessionLocation, declared_name=None):
    """A wire location bound to the root whose registry decoded it."""

    root: Annotated[Path, WireTransient()] = field(repr=False)

    def bound_to(self, root: Path) -> BoundSessionLocation:
        """Publishing into another root keeps the transcript this location names."""
        if self.root == root or self.root == (root := root.resolve()):
            return self
        return self.publish(self.resolve(self.root), root, self.publisher(self.root))

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return self.located(backend, self.resolve(self.root))

    def writable_location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return self.located(backend, self.writable(self.root))

    @staticmethod
    def located(backend: type[AgentBackend], path: Path) -> RuntimeLocation:
        return backend.location_type(str(path))


@dataclass(frozen=True, slots=True, eq=False)
class BusSessionLocation(RegistrySessionLocation):
    """A transcript stored under the bus root; a copied root reads its own copy."""

    def __post_init__(self) -> None:
        if self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError(f"Bus session must be relative to its root: {self.path}")

    def bound_to(self, root: Path) -> BoundSessionLocation:
        return BoundBusSession(self.path, root.resolve())

    def resolve(self, root: Path) -> Path:
        root = root.resolve()
        path = (root / self.path).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Bus session escapes its root: {self.path}")
        return path

    def publisher(self, root: Path) -> Path:
        return root.resolve()


@dataclass(frozen=True, eq=False)
class ExternalSessionLocation(RegistrySessionLocation):
    """A harness transcript outside every bus root; only the root that published it writes it.

    A copied registry keeps the published root, so the copy reads the transcript and refuses
    to write it."""

    published: Annotated[Path, PathText]

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or not self.published.is_absolute():
            raise ValueError(
                f"External session must be absolute: {self.path} from {self.published}"
            )

    def bound_to(self, root: Path) -> BoundSessionLocation:
        return BoundExternalSession(self.path, self.published, root.resolve())

    def resolve(self, root: Path) -> Path:
        return self.path

    def publisher(self, root: Path) -> Path:
        return self.published

    def writable(self, root: Path) -> Path:
        if root.resolve() != self.published.resolve():
            raise RelationViolationError(
                f"Copied bus {root} cannot write external session {self.path} "
                f"published by {self.published}"
            )
        return self.resolve(root)


@dataclass(frozen=True, eq=False)
class BoundBusSession(BoundSessionLocation, BusSessionLocation, declared_name=None):
    pass


@dataclass(frozen=True, eq=False)
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
