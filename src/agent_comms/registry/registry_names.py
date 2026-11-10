"""Original namespace resolution, below live activity and snapshot assembly."""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
from agent_comms_hooks import ToolRefusedError

Member = TypeVar("Member", bound=ThreadIdentitySource, covariant=True)


@dataclass(frozen=True, slots=True)
class DeletedThread:
    """A participant whose declaration was permanently deleted.

    Its messages remain on the bus. It has no birth, owner or transcript;
    readers show its recorded name and nothing else.
    """

    name: str

    def answering_thread[Reader](self, reader: Reader) -> Reader:
        """No transcript survives deletion, so what it owed falls to whoever reads the debt."""
        return reader

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

    def taken(self, name: str, owner: str | None = None) -> bool:
        """A canonical name or permanent alias occupies this text, and it is not one of
        ``owner``'s own names; with no owner, any occupied text is taken."""
        return (name in self.threads or name in self.aliases) and self.canonical_name(name) != owner

    def require_fresh_name(self, name: str) -> None:
        """A new declaration cannot replace a current participant or permanent alias."""
        if self.taken(name):
            raise RelationViolationError(f"Thread name {name!r} is already reserved.")

    def has_invalid_aliases(self, names: Iterable[str]) -> bool:
        """Check the selected alias edges directly against this namespace's declarations."""
        return any(self.aliases[alias] not in self.threads or alias in self.threads
                   for alias in names if alias in self.aliases)

    def require_alias_names(self) -> None:
        """Every alias names one retained declaration and occupies no declaration's name."""
        if self.has_invalid_aliases(self.aliases):
            raise RelationViolationError("alias has no exclusive retained owner")

    @staticmethod
    def thread_slug(text: str) -> str:
        """Characters a name cannot hold become "-", trimmed; empty text names a session."""
        return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "session"

    def claim_name(self, wanted: str, owner: str | None = None) -> str:
        """The first free thread name for wanted text: its slug, then slug-2, slug-3.

        A name that already resolves to ``owner`` is free for that owner, so a
        rename to one of its own names keeps it.
        """
        base = self.thread_slug(wanted)
        name, suffix = base, 2
        while self.taken(name, owner):
            name, suffix = f"{base}-{suffix}", suffix + 1
        return name

    def named_members(self) -> Iterator[tuple[str, Member]]:
        """Original declarations under their canonical and retained alias names."""
        yield from self.threads.items()
        for alias in self.aliases:
            yield alias, self.require(alias)

    def named_incarnations(self) -> Iterator[tuple[str, ThreadIncarnation]]:
        """Every current and retained name with its identity."""
        for name, source in self.named_members():
            yield name, source.incarnation

    def names_for(self, name: str) -> frozenset[str]:
        """Current and retained names of one declared member."""
        canonical = self.canonical_name(name)
        return frozenset({canonical, *(
            alias for alias, target in self.aliases.items() if target == canonical
        )})

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
            raise self.unregistered(canonical) from error

    def unregistered(self, name: str) -> ToolRefusedError:
        """The refusal for a name this namespace does not hold."""
        return UnregisteredThreadError(f"Thread {name!r} is not registered.")
