"""One source's recorded namespace, without live execution/status authority."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from functools import cached_property, partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar

from agent_comms.field_codec import ThreadLookup

from .agent_activity import UnavailableRecipientActivity
from .errors import RelationViolationError, UnregisteredThreadError
from .routing import DeliveryScope
from .thread_identity import ThreadIncarnation
from .threads import ThreadProvenance

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .threads import Thread

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


class RecipientSelection(ABC):
    """One registry selection owns live, archived and retired recipient behavior."""

    @abstractmethod
    def activity(self, observations, snapshot): ...

    @abstractmethod
    def read_effect(self, reads, message, snapshot, *, document=None): ...


@dataclass(frozen=True, slots=True)
class KnownRecipient(RecipientSelection):
    thread: ThreadProvenance

    def read_effect(self, reads, message, snapshot, *, document=None):
        return snapshot.displayed_recipient_effect(
            reads, message, self.thread, document=document
        )


@dataclass(frozen=True, slots=True)
class RegisteredRecipient(KnownRecipient):
    thread: Thread

    def activity(self, observations, snapshot):
        return self.thread.execution.observe_recipient(observations, snapshot, self.thread)


@dataclass(frozen=True, slots=True)
class ArchivedRecipient(KnownRecipient):
    def activity(self, observations, snapshot):
        raise RelationViolationError("Archived recipients have no live activity")


@dataclass(frozen=True, slots=True)
class RetiredRecipient(RecipientSelection):
    lookup: ThreadLookup

    def activity(self, observations, snapshot):
        return UnavailableRecipientActivity()

    def read_effect(self, reads, message, snapshot, *, document=None):
        return ()


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
            raise UnregisteredThreadError(f"Thread {canonical!r} is not registered.") from error


@dataclass(frozen=True, kw_only=True)
class RegistryProvenance(RegistryNames[ThreadProvenance]):
    threads: dict[str, ThreadProvenance]
    aliases: dict[str, str]

    def __post_init__(self) -> None:
        if any(name != thread.name for name, thread in self.threads.items()):
            raise RelationViolationError("Provenance name differs from its original declaration")
        self.require_alias_names()

    def require_alias_names(self) -> None:
        names = set(self.threads)
        if any(target not in names or alias in names for alias, target in self.aliases.items()):
            raise RelationViolationError("Provenance alias has no exclusive retained owner")

    def bound_to(self, root: Path) -> RegistryProvenance:
        """Recorded transcripts resolve under the root this namespace was captured from."""
        threads = {name: replace(thread, session_file=thread.session_file.bound_to(root))
                   for name, thread in self.threads.items()}
        if all(threads[name].session_file is thread.session_file
               for name, thread in self.threads.items()):
            return self
        return replace(self, threads=threads)

    @cached_property
    def sender_bindings(self) -> Mapping[str, str]:
        """One lazy SQL value for this namespace, including its historical names."""
        senders = {}
        unrecorded: list[str] = []
        for name, source in self.named_members():
            senders[name] = tuple(DeliveryScope.sender_lookups(
                source.incarnation, partial(unrecorded.append, name)
            ))
        return MappingProxyType({
            "senders": json.dumps(senders), "unrecorded": json.dumps(unrecorded),
        })

    def retired(self, name: str) -> bool:
        """A recorded namespace keeps no removal ledger. A name its own bus
        carries but its captured registry lacks was deleted before capture."""
        return name not in self.threads

    def unread_since(
        self, actor: str, since: float | None, recorded_delivery: bool,
    ) -> float | None:
        """An archived namespace supplies display history, never native input grants."""
        return since

    def displayed_recipient_effect(self, reads, message, thread: ThreadProvenance, *, document=None):
        return reads.displayed_incarnation(message, thread.incarnation, self, document=document)

    def historical_recipient(self, recipient: FrozenRecipient) -> ArchivedRecipient | RetiredRecipient:
        """Archives select by original name and birth; duplicate births stay distinct."""
        thread = self.threads.get(self.canonical_name(recipient.canonical_thread))
        if thread is not None and thread.incarnation.selects_lookup(recipient.recipient_lookup):
            return ArchivedRecipient(thread)
        return RetiredRecipient(recipient.recipient_lookup)

    @staticmethod
    def capture(source: RegistryNames[ThreadProvenance]) -> RegistryProvenance:
        return RegistryProvenance(
            threads={
                name: ThreadProvenance.capture(thread) for name, thread in source.threads.items()
            },
            aliases=dict(source.aliases),
        )
