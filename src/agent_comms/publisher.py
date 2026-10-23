"""Publication decisions over the canonical Bus and registered audience."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from functools import partial
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, BusReader, BusSnapshot, ClaimRecord, PrivateRecord, RecordBuilder
from agent_comms.bus_publication import InitialOrigin
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)
from agent_comms.field_codec import Id128Text
from agent_comms.messages import ClaimNoticeKind, Message
from agent_comms.response_acknowledgement import canonical_publication_key

from .active_route import guard_original_root_write
from .bus_activity_index import ActivityClocks
from .channel_targets import CommandTarget, NamedTarget
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .envelope_claim_transitions import (
    ClaimOwner,
    ClaimRelease,
    ClaimTransition,
    RecordedScope,
    ReleasedClaims,
    WakeClaims,
)
from .errors import (
    ClaimEnvelopeUnknownError,
    RelationViolationError,
    UnregisteredThreadError,
)
from .mentions import ThreadMention
from .private_path import FileRevision
from .registry_document import DEFAULT_ROSTER, RegistrySnapshot
from .wake import ControlClassification, resolve_wake_cohort

if TYPE_CHECKING:
    from .coordination_session import NativeBoundary
    from .registration import Registration
    from .threads import Thread

from .catalog_document import CatalogDocument
from .catalog_store import ChannelCatalog
from .channels import Channel


@dataclass(eq=False, repr=False)
class Publisher:
    """Reads and validates publications; each private write is a capability below."""

    bus: Bus
    registry: Registration
    channels: ChannelCatalog
    opened_activity: Callable[[], AbstractContextManager[ActivityClocks]]


    def _validate_publish_request(
        self, message: Message, *, registry_snapshot: RegistrySnapshot,
        catalog: CatalogDocument, activity: ActivityClocks,
        destination: NamedTarget,
    ) -> tuple[str, NamedTarget, Callable[[str], Channel]]:
        """Resolve the public route from the caller's acquired declarations."""

        snapshot = registry_snapshot
        snapshot.purpose.require_write()
        sender = snapshot.canonical_name(message.sender)
        if sender not in snapshot.threads:
            raise UnregisteredThreadError(f"Sender {message.sender!r} is not a registered thread.")
        target = destination
        require_channel = partial(catalog.require_channel, activity=activity)
        target.require_publication(message, snapshot, require_channel)
        return sender, target, require_channel

    def _prepare_message_unlocked(
        self,
        message: Message,
        *,
        sender: str,
        target: NamedTarget,
        sequence: int,
        snapshot: RegistrySnapshot,
        original_source: BusReader,
    ) -> Message:
        message.require_task_publication(sender, snapshot, original_source)

        def resolve_mention(name: str) -> str | None:
            return next((thread.name for thread in
                         snapshot.visible_threads(DEFAULT_ROSTER, names=(name,))), None)

        return replace(
            message,
            sender=sender,
            target=target.name,
            seq=sequence,
            sender_role=snapshot.threads[sender].role,
            mentions=ThreadMention.find(message.body, resolve_mention),
        )


class ClaimWrites(Publisher):
    """Publishes claim envelopes."""

    def release_resources(
        self, boundary: NativeBoundary, claims: Sequence[ClaimOwner], wording: str,
    ) -> None:
        """Release only still-current acquisitions, in bounded whole-set envelopes.

        The resource lifetime supplies its original acquisition witnesses. A
        delayed retirement cannot release a successor's generation or a manual
        claim absent from that lifetime. The caller holds the native boundary.
        """
        with self.bus.reading() as source:
            owned = tuple(claim for claim in claims if source.resource_owner(claim.resource) == claim)
        ordered = sorted(owned, key=lambda claim: claim.incarnation)
        for incarnation, records in groupby(ordered, key=lambda claim: claim.incarnation):
            recorded = tuple(records)
            owner = boundary.registry.require(recorded[0].owner)
            worktree = Path(owner.worktree).absolute()
            for offset in range(0, len(recorded), ClaimTransition.max_resources):
                released = recorded[offset:offset + ClaimTransition.max_resources]
                paths = (Path(claim.resource) for claim in released)
                names = ", ".join(str(path.relative_to(worktree) if path.is_relative_to(worktree)
                                      else path) for path in paths)
                self._publish_claim_transition_unlocked(
                    boundary.registry,
                    Message(owner.name, owner.name, f"{wording}: {names}", ClaimNoticeKind),
                    partial(ReleasedClaims, incarnation=incarnation,
                            releases=tuple(ClaimRelease(claim.resource, claim.generation)
                                           for claim in released)), activity=boundary.activity)

    def _claim_builder(
        self, snapshot: RegistrySnapshot, message: Message,
        make_transition: Callable[..., ClaimTransition], *, activity: ActivityClocks,
    ) -> RecordBuilder:
        sender, target, require_channel = self._validate_publish_request(
            message, registry_snapshot=snapshot, catalog=self.channels.read(), activity=activity,
            destination=CommandTarget.named(message.target))

        def build(seq: int, db: sqlite3.Connection) -> ClaimRecord:
            stored = self._prepare_message_unlocked(
                message, sender=sender, target=target, sequence=seq, snapshot=snapshot,
                original_source=BusSnapshot.from_connection(db),
            )
            transition = make_transition(owner=stored.sender, seq=seq,
                                         message_id=stored.message_id)
            return ClaimRecord.of(stored, self.bus.prepare_claim(db, transition, snapshot))

        return build

    def _claim_result[Result](self, publish: Callable[[], Result]) -> Result:
        try:
            return publish()
        except (OSError, RelationViolationError) as error:
            raise ClaimEnvelopeUnknownError(
                "Claim envelope outcome UNKNOWN; inspect durable bus; do not replay."
            ) from error

    def _publish_acquisition_unlocked(
        self, snapshot: RegistrySnapshot, owner: Thread,
        resource: str, claim: RecordedScope, *, activity: ActivityClocks,
    ) -> ClaimOwner:
        build = self._claim_builder(
            snapshot, ClaimNoticeKind.claimed(owner),
            partial(WakeClaims, incarnation=str(owner.created_at), claims=(resource,),
                    generation=Id128Text.mint(), claim_scope=claim), activity=activity,
        )
        return self._claim_result(lambda: self.bus.acquire_resource_claim(resource, claim, snapshot, build))

    def _publish_claim_transition_unlocked(
        self, snapshot: RegistrySnapshot, message: Message,
        make_transition: Callable[..., ClaimTransition], *, activity: ActivityClocks,
    ) -> ClaimTransition:
        """One typed transition, checked and appended under the registry cut."""
        build = self._claim_builder(snapshot, message, make_transition, activity=activity)
        return self._claim_result(lambda: self.bus.append(build).require_claim_transition())


class InitialWrites(Publisher):
    """Publish an initial message and its frozen delivery decisions together."""

    def publish_ordinary(self, message: Message, *, origin: InitialOrigin,
                         destination: NamedTarget) -> Message:
        """Hold the registry/catalog cut; check source conditions in the append transaction."""
        with (
            self.opened_activity() as activity,
            guard_original_root_write(self.bus.path.parent),
            self.channels.reading(self.registry.store) as catalog,
        ):
            snapshot = self.registry.snapshot()
            sender, target, require_channel = self._validate_publish_request(
                message, registry_snapshot=snapshot, catalog=catalog, activity=activity,
                destination=destination)
            sender_thread = snapshot.require(sender)
            snapshot.status(sender).require_visible()
            # The kind owns its own validation; one publication check, before
            # any sequence reservation, refusing with what is missing.
            message.type.require_publishable(message, Path(sender_thread.worktree))
            origin.require_sender(message, sender_thread, snapshot)
            target, message, names = target.initial_delivery(
                sender, message, snapshot, require_channel)
            selected = [snapshot.threads[name] for name in names]
            lookups = [thread.incarnation.lookup for thread in selected]
            sender_lookup = snapshot.threads[sender].incarnation.lookup
            # Participant identities are prerequisites, not part of the bus commit.
            # Finish their own transactions before opening the publication transaction.
            with Coordination(str(CoordinationStore.path_in(self.bus.path.parent))) as store:
                sender_thread.role.register_sender(store.participants, sender_lookup, sender)
                for thread, lookup in zip(selected, lookups, strict=True):
                    store.participants.register(lookup, thread.name, thread.name, committed=True)

            def build(seq: int, db: sqlite3.Connection) -> PrivateRecord:
                source = BusSnapshot.from_connection(db)
                stored = self._prepare_message_unlocked(
                    message, sender=sender, target=target, sequence=seq,
                    snapshot=snapshot, original_source=source,
                )
                origin.require_source(source, sender, stored.message_id)
                revision = hashlib.sha256(
                    repr((
                        FileRevision.capture(self.registry.store.path),
                        self.channels.revision(),
                        sorted((name, thread.created_at, sorted(thread.tags))
                               for name, thread in snapshot.threads.items()),
                    )).encode()
                ).hexdigest()
                audience = freeze_audience(
                    stored,
                    tuple(FrozenRecipient(lookup, name)
                          for lookup, name in zip(lookups, names, strict=True)),
                    revision, sender_lookup=sender_lookup, sender_name=sender,
                )
                decisions = resolve_wake_cohort(
                    stored, frozen_audience=audience, control=ControlClassification.ORDINARY,
                )
                return PrivateRecord.from_message(
                    stored, audience=audience, decisions=decisions,
                    control=ControlClassification.ORDINARY.value,
                )

            return origin.append(self.bus, build)


class ResponseWrites(Publisher):
    """Publishes keyed responses."""

    def _publish_keyed_response_unlocked(
        self,
        intent: PublicationIntents,
        *,
        conversation,
        registry_snapshot: RegistrySnapshot,
        activity: ActivityClocks,
    ) -> Message:
        """Publish against the caller's held registry snapshot."""

        matched = intent.published_message(self.bus)
        if matched is not None:
            return matched
        message = intent.message
        # A durable exact replay above wins even after registry/route renames.
        sender, target, require_channel = self._validate_publish_request(
            message, registry_snapshot=registry_snapshot, catalog=self.channels.read(), activity=activity,
            destination=CommandTarget.named(message.target),
        )
        role = registry_snapshot.threads[sender].role
        role.require_executable(RelationViolationError("Keyed response sender must be executable."))
        if intent.publication_key != canonical_publication_key(intent.execution_id, target.name):
            raise RelationViolationError("Response publication key does not match its route.")
        def build(seq: int, db: sqlite3.Connection):
            stored = self._prepare_message_unlocked(
                message, sender=sender, target=target, sequence=seq, snapshot=registry_snapshot,
                original_source=BusSnapshot.from_connection(db),
            )
            if not intent.matches_publication(stored):
                raise RelationViolationError("Stored response does not match publication intent.")
            return conversation.record(seq, stored, intent)

        return self.bus.append(build)


class OwnerPublisher(InitialWrites, ClaimWrites):
    """An owner's bus: initial sends and resource claims."""


class ParticipantPublisher(ResponseWrites, ClaimWrites):
    """A selected participant's bus: its keyed responses and its claims."""
