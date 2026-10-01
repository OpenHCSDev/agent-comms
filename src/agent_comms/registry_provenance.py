"""One source's recorded namespace, without live execution/status authority."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RelationViolationError, UnregisteredThreadError
from .thread_provenance import ThreadProvenance


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistryProvenance:
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

    def require(self, name: str) -> ThreadProvenance:
        canonical = self.aliases.get(name, name)
        try:
            return self.threads[canonical]
        except KeyError as error:
            raise UnregisteredThreadError(f"Thread {canonical!r} is not registered.") from error
