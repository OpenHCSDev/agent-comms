"""One source's recorded namespace, without live execution/status authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from .errors import RelationViolationError, UnregisteredThreadError
from .thread_provenance import ThreadProvenance


Member = TypeVar("Member", bound=ThreadProvenance, covariant=True)


class RegistryNames(Generic[Member]):
    """Shared resolution behavior for live and recorded registry namespaces."""

    __slots__ = ()

    aliases: Mapping[str, str]
    threads: Mapping[str, Member]

    def canonical_name(self, name: str) -> str:
        """Resolve this namespace without requiring current membership."""
        return self.aliases.get(name, name)

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

    @staticmethod
    def capture(snapshot: RegistryProvenance) -> RegistryProvenance:
        return RegistryProvenance(
            threads={name: ThreadProvenance.capture(thread) for name, thread in snapshot.threads.items()},
            aliases=dict(snapshot.aliases),
        )
