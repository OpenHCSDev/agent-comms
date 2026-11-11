from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import (
    dataclass,
    replace,
)
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    cast,
)

from pydantic import JsonValue

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import (
    FieldCodec,
    FieldRepresentation,
    NullMember,
    PathText,
    RecordMember,
    SubclassUnion,
    WireOmitDefault,
)
from agent_comms.projects.repository_name import RepositoryName

if TYPE_CHECKING:
    from agent_comms.projects.storage_rules import StorageRule

StoragePath = Annotated[Path, PathText]


class ThreadStorage(SubclassUnion, affix="ThreadStorage"):
    """A thread's storage roots: the ones its registry assigned, or none assigned yet."""

    @abstractmethod
    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        """The roots thread ``name`` holds under ``rule``."""

    @abstractmethod
    def stored(self) -> StorageRoots:
        """The roots a registry row records."""


class CheckoutReuse(ABC):
    """One request to reuse an owner's existing tree for a repository. A RepositoryChoice
    answers it; the storage rule that can check trees implements it."""

    @abstractmethod
    def selected(self, path: Path) -> Path:
        """The chosen tree, once it is still an owned editable checkout of the repository."""

    @abstractmethod
    def unselected(self) -> Path:
        """The tree reused when no choice was stored."""


class RepositoryChoice(ABC):
    """Which owned editable checkout a thread reuses for one repository."""

    __slots__ = ()

    @abstractmethod
    def reused_checkout(self, reuse: CheckoutReuse) -> Path:
        """The checkout this choice reuses, or the reuse's fresh path when no owned tree exists."""


@dataclass(frozen=True, slots=True)
class SelectedCheckout(RecordMember, RepositoryChoice):
    """One repository's editable checkout, chosen independently of navigation."""

    repository: RepositoryName
    path: StoragePath

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("A selected checkout requires an absolute path.")

    def reused_checkout(self, reuse: CheckoutReuse) -> Path:
        return reuse.selected(self.path)


@dataclass(frozen=True, slots=True)
class NoRepositoryChoice(RepositoryChoice):
    """No explicit choice: the current project, else the sole owned tree, else ``fresh``."""

    def reused_checkout(self, reuse: CheckoutReuse) -> Path:
        return reuse.unselected()


@dataclass(frozen=True, slots=True)
class StorageRoots(RecordMember, ThreadStorage):
    scratch: StoragePath
    selected_checkouts: Annotated[tuple[SelectedCheckout, ...], WireOmitDefault()] = ()

    def __post_init__(self) -> None:
        if not self.scratch.is_absolute():
            raise ValueError("Storage scratch requires an absolute path.")
        repositories = {choice.repository for choice in self.selected_checkouts}
        if len(repositories) != len(self.selected_checkouts):
            raise ValueError("A repository can have only one selected checkout.")

    def choice(self, repository: RepositoryName) -> RepositoryChoice:
        return next((choice for choice in self.selected_checkouts
                     if choice.repository == repository), NoRepositoryChoice())

    def choosing(self, repository: RepositoryName, path: Path) -> StorageRoots:
        return replace(self, selected_checkouts=(*(
            choice for choice in self.selected_checkouts if choice.repository != repository
        ), SelectedCheckout(repository, path)))

    def relocated(self, source: Path, destination: Path) -> StorageRoots:
        return replace(self, selected_checkouts=tuple(
            replace(choice, path=destination) if choice.path == source else choice
            for choice in self.selected_checkouts))

    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        return self

    def stored(self) -> StorageRoots:
        return self

    @property
    def allocation_name(self) -> str:
        """The name the registry gave this thread's storage at registration. Its checkouts
        carry it, so a renamed thread still owns the work it allocated."""
        return self.scratch.name


@dataclass(frozen=True, slots=True)
class UnassignedThreadStorage(NullMember, ThreadStorage):
    """A thread not yet declared; its registry assigns its roots at declaration."""

    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        return rule.initial_roots(name)

    def stored(self) -> StorageRoots:
        raise RelationViolationError(
            "A thread is stored only after its registry assigned its storage roots."
        )


class AssignedStorageRoots(FieldRepresentation):
    """A stored thread holds the roots its registry assigned at registration, so its
    stored form is StorageRoots; only an unregistered thread in memory has none."""

    @classmethod
    def decode(cls, value: object) -> StorageRoots:
        return FieldCodec.decode(StorageRoots, value)

    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(cast(ThreadStorage, value).stored())

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(StorageRoots)
