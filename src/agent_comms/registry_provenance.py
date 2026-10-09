"""One source's recorded namespace, without live execution/status authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from .errors import RelationViolationError, UnregisteredThreadError
from .thread_identity import ThreadIncarnation
from .thread_provenance import ThreadProvenance


Member = TypeVar("Member", bound=ThreadProvenance, covariant=True)


@dataclass(frozen=True, slots=True)
class DeletedThread:
    """A participant whose declaration was permanently deleted.

    Its messages remain on the bus. It has no birth, owner or transcript;
    readers show its recorded name and nothing else.
    """

    name: str

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation.deleted(self.name)


class RegistryNames(Generic[Member]):
    """Shared resolution behavior for live and recorded registry namespaces."""

    __slots__ = ()

    aliases: Mapping[str, str]
    threads: Mapping[str, Member]

    def canonical_name(self, name: str) -> str:
        """Resolve this namespace without requiring current membership."""
        return self.aliases.get(name, name)

    def retired(self, name: str) -> bool:
        """Whether this namespace records that canonical ``name`` was declared and removed.

        Only a snapshot or a recorded namespace answers; the mutable document does not.
        """
        raise NotImplementedError(f"{type(self).__name__} keeps no deletion record")

    def participant(self, name: str) -> Member | DeletedThread:
        """Resolve a message participant: a declaration, or a deleted thread.

        A name this namespace never declared is not a participant; it fails.
        """
        canonical = self.canonical_name(name)
        thread = self.threads.get(canonical)
        if thread is not None:
            return thread
        if self.retired(canonical):
            return DeletedThread(canonical)
        raise UnregisteredThreadError(f"Thread {canonical!r} was never registered.")

    def require(self, name: str) -> Member:
        canonical = self.canonical_name(name)
        try:
            return self.threads[canonical]
        except KeyError as error:
            raise UnregisteredThreadError(f"Thread {canonical!r} is not registered.") from error


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistryProvenance(RegistryNames[ThreadProvenance]):
    threads: dict[str, ThreadProvenance]
    aliases: dict[str, str]

    def __post_init__(self) -> None:
        names = set(self.threads)
        if any(name != thread.name for name, thread in self.threads.items()):
            raise RelationViolationError("Provenance name differs from its original declaration")
        if any(target not in names or alias in names for alias, target in self.aliases.items()):
            raise RelationViolationError("Provenance alias has no exclusive retained owner")

    def retired(self, name: str) -> bool:
        """A recorded namespace keeps no removal ledger. A name its own bus
        carries but its captured registry lacks was deleted before capture."""
        return name not in self.threads

    @staticmethod
    def capture(snapshot: RegistryProvenance) -> RegistryProvenance:
        return RegistryProvenance(
            threads={name: ThreadProvenance.capture(thread) for name, thread in snapshot.threads.items()},
            aliases=dict(snapshot.aliases),
        )
