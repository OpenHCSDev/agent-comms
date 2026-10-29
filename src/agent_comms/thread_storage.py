"""Stored thread storage declarations, independent of enforcement and live process state."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

from pydantic import JsonValue

from .errors import RelationViolationError
from agent_comms.nominal.field_codec import (
    FieldCodec,
    FieldRepresentation,
    NullMember,
    PathText,
    RecordMember,
    SubclassUnion,
)

if TYPE_CHECKING:
    from .storage_rules import StorageRule

StoragePath = Annotated[Path, PathText]


class ThreadStorage(SubclassUnion, affix="ThreadStorage"):
    """A thread's storage roots: the ones its registry assigned, or none assigned yet."""

    @abstractmethod
    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        """The roots thread ``name`` holds under ``rule``."""

    @abstractmethod
    def stored(self) -> StorageRoots:
        """The roots a registry row records."""


@dataclass(frozen=True, slots=True)
class StorageRoots(RecordMember, ThreadStorage):
    scratch: StoragePath

    def __post_init__(self) -> None:
        if not self.scratch.is_absolute():
            raise ValueError("Storage scratch requires an absolute path.")

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
