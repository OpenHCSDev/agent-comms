"""One source's recorded namespace, without live execution/status authority."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import cached_property, partial
from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.message_sender import PublicSender
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.registry.namespace_reads import NamespaceRead
from agent_comms.registry.recipient_activity import UnavailableRecipientActivity
from agent_comms.registry.recipient_selection import RecipientSelection
from agent_comms.registry.registry_names import RegistryNames as RegistryNames
from agent_comms.registry.threads import Thread, ThreadProvenance


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

    def activity_threads(self) -> tuple[Thread, ...]:
        return (self.thread,)

    def activity(self, observations, snapshot):
        """The owner binding decides whether this recipient's activity is observable."""
        return self.thread.owner_binding(snapshot).recipient_activity(
            observations, snapshot, self.thread)

    def input_owners(self) -> tuple[Thread, ...]:
        return (self.thread,)


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


@dataclass(frozen=True, slots=True)
class SenderBindings:
    """The namespace's SQL inputs, built together from its original members once per snapshot,
    each sender's part encoded on its own so a read binds only the senders it can meet."""

    senders: Mapping[str, str]
    births: Mapping[str, str]
    unrecorded: tuple[str, ...]

    def bound(self, routed: Iterable[str]) -> tuple[str, str, str]:
        """The senders, unrecorded names and births of the senders whose routes reach a read's
        targets: the only ones its route keys can join."""
        named = [name for name in dict.fromkeys(routed) if name in self.senders]
        born = [name for name in dict.fromkeys((*named, *self.unrecorded)) if name in self.births]
        return (
            "{" + ",".join(f"{json.dumps(name)}:{self.senders[name]}" for name in named) + "}",
            json.dumps(self.unrecorded),
            "{" + ",".join(f"{json.dumps(name)}:{self.births[name]}" for name in born) + "}",
        )


@dataclass(frozen=True, kw_only=True)
class RegistryProvenance(NamespaceRead[ThreadProvenance, RecipientSelection]):
    threads: dict[str, ThreadProvenance]
    aliases: dict[str, str]

    def __post_init__(self) -> None:
        if any(name != thread.name for name, thread in self.threads.items()):
            raise RelationViolationError("Provenance name differs from its original declaration")
        self.require_alias_names()

    def bound_to(self, root: Path) -> RegistryProvenance:
        """Recorded transcripts resolve under the root this namespace was captured from."""
        threads = {name: replace(thread, session_file=thread.session_file.bound_to(root))
                   for name, thread in self.threads.items()}
        if all(threads[name].session_file is thread.session_file
               for name, thread in self.threads.items()):
            return self
        return replace(self, threads=threads)

    @cached_property
    def sender_bindings(self) -> SenderBindings:
        """Current sender identities and births for SQL reads, including aliases."""
        senders = {}
        births = {}
        unrecorded: list[str] = []

        def missing_birth(name: str) -> tuple[ThreadLookup, ...]:
            unrecorded.append(name)
            return ()

        for name, incarnation in self.named_incarnations():
            births[name] = json.dumps(incarnation.wire_birth)
            public = PublicSender()
            recorded = incarnation.recorded_or(
                lambda entry: (entry.lookup,), partial(missing_birth, name),
            )
            senders[name] = FieldCodec.compact(FieldCodec.encode(
                (public, *recorded),
                tuple[ThreadLookup | PublicSender, ...],
            ))
        return SenderBindings(senders=senders, births=births, unrecorded=tuple(unrecorded))

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

    def historical_recipient(
        self, recipient: FrozenRecipient,
    ) -> ArchivedRecipient | RetiredRecipient:
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
