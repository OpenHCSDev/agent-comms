"""Publication decisions over one canonical WireLog and registered audience."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)

from agent_comms.response_acknowledgement import canonical_publication_key

from .active_route import guard_original_root_write
from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, ClaimRecord, PrivateRecord, PublicRecord
from .bus_activity_index import ActivityClocks
from agent_comms.bus_publication import InitialOrigin
from .channel_targets import CommandTarget, NamedTarget
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .envelope_claim_transitions import (
    ClaimOwner,
    ClaimRelease,
    ClaimScope,
    ClaimTransition,
    ReleasedClaims,
    WakeClaims,
)
from .errors import (
    ClaimEnvelopeUnknownError,
    RelationViolationError,
    UnregisteredThreadError,
)
from agent_comms.field_codec import Id128Text
from .mentions import ThreadMention
from agent_comms.messages import ClaimNoticeKind, Message
from .private_bus_checkpoint import (
    install_private_bus_checkpoint,
)
from .private_path import FileRevision
from .private_registry_guard import PrivateRegistryGuard, _require_no_private_owner_rename
from .registry_document import DEFAULT_ROSTER, RegistrySnapshot
from agent_comms.store_files import _store_lock
from .wake import ControlClassification, resolve_wake_cohort

if TYPE_CHECKING:
    from .coordination_session import NativeBoundary
    from .private_bus_checkpoint import CheckedRead
    from .registration import Registration
    from .threads import Thread

from .catalog_document import CatalogDocument
from .catalog_store import ChannelCatalog
from .channels import Channel
from .wire_log import WireLog, WireStoreLock
from .wire_metadata import WireMetadata


class Publisher:
    """Reads and validates publications; each private write is a capability below."""

    def __init__(self, log: WireLog, registry: Registration, channels: ChannelCatalog, opened_activity):
        self.log = log
        self.bus = Bus(Bus.path_in(log.path.parent))
        self._registry = registry
        self._channels = channels
        self.opened_activity = opened_activity

    @classmethod
    def prepare_root(cls, root: Path) -> None:
        """A publisher that only reads leaves its root as it finds it."""

    def _validate_publish_request(
        self, message: Message, *, registry_snapshot: RegistrySnapshot,
        catalog: CatalogDocument, activity: ActivityClocks,
    ) -> tuple[str, NamedTarget, Callable[[str], Channel]]:
        """Resolve the public route from the caller's acquired declarations."""

        snapshot = registry_snapshot
        sender = snapshot.canonical_name(message.sender)
        if sender not in snapshot.threads:
            raise UnregisteredThreadError(f"Sender {message.sender!r} is not a registered thread.")
        target = CommandTarget.named(message.target)
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
        original_source: CheckedRead | None = None,
    ) -> Message:
        if original_source is not None:
            original_source.require_marker(self.log.private_marker_unlocked())
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


class PrivateWrites(Publisher):
    """A publisher that makes some private write; its root exists before it writes."""

    @classmethod
    def prepare_root(cls, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)


class ClaimWrites(PrivateWrites):
    """Publishes claim envelopes."""

    def release_resources(
        self, boundary: NativeBoundary, claims: Sequence[ClaimOwner], wording: str,
    ) -> None:
        """Release only still-current acquisitions, in bounded whole-set envelopes.

        The resource lifetime supplies its original acquisition witnesses. A
        delayed retirement cannot release a successor's generation or a manual
        claim absent from that lifetime. The caller holds the native boundary.
        """
        metadata = self.log.private_marker_unlocked()
        with boundary.bus_lock.current_source(self.log, metadata) as source:
            owned = tuple(claim for claim in claims if source.claim(claim.resource) == claim)
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
                    boundary.bus_lock, boundary.registry,
                    Message(owner.name, owner.name, f"{wording}: {names}", ClaimNoticeKind),
                    partial(ReleasedClaims, incarnation=incarnation,
                            releases=tuple(ClaimRelease(claim.resource, claim.generation)
                                           for claim in released)), activity=boundary.activity)

    def _publish_acquisition_unlocked(
        self, bus_lock: WireStoreLock, snapshot: RegistrySnapshot, owner: Thread,
        resource: str, claim: RecordedScope, *, activity: ActivityClocks,
    ) -> ClaimOwner:
        def publish() -> ClaimOwner:
            transition = self._publish_claim_transition_unlocked(
                bus_lock, snapshot, ClaimNoticeKind.claimed(owner),
                partial(WakeClaims, incarnation=str(owner.created_at), claims=(resource,),
                        generation=Id128Text.mint(), claim_scope=claim), activity=activity)
            return transition.claimed(resource)
        with bus_lock.current_source(self.log, self.log.private_marker_unlocked()) as source:
            previous = source.claim(resource)
        return previous.acquire(claim, snapshot, publish)

    def _publish_claim_transition_unlocked[Change: ClaimTransition](
        self, bus_lock: WireStoreLock, snapshot: RegistrySnapshot, message: Message,
        make_transition: Callable[..., Change], *, activity: ActivityClocks,
    ) -> Change:
        """One typed transition, checked before append under the registry cut."""
        metadata = self.log.private_marker_unlocked()
        if not metadata.claims:
            raise RelationViolationError("Claim read barrier is unavailable.")
        sender, target, require_channel = self._validate_publish_request(message, registry_snapshot=snapshot, catalog=self._channels.read(), activity=activity)
        with bus_lock.current_source(self.log, metadata) as source:
            def build(seq: int) -> ClaimRecord:
                stored = self._prepare_message_unlocked(
                    message, sender=sender, target=target, sequence=seq, snapshot=snapshot,
                )
                transition = make_transition(owner=stored.sender, seq=seq,
                                             message_id=stored.message_id)
                return ClaimRecord.of(stored, source.prepare_claim(transition, snapshot))

            try:
                return self.bus.append(build).claim_transition
            except (OSError, RelationViolationError) as error:
                raise ClaimEnvelopeUnknownError(
                    "Claim envelope outcome UNKNOWN; inspect durable bus; do not replay."
                ) from error


class InitialWrites(PrivateWrites):
    """Publishes ordinary sends and initial cohorts, and issues a fresh root's protocol."""

    def publish_ordinary(self, message: Message, *, origin: InitialOrigin) -> Message:
        """Publish through the canonical audience/decision writer.

        A truly fresh root is initialized once under the bus lock. Existing
        unmarked data requires the explicit archival cutover; a send never
        rewrites history or falls back to an uncoordinated append.
        """

        with self.opened_activity() as activity, guard_original_root_write(self.log.path.parent), self.log.locked() as bus_lock:
            if not self.log.read_metadata_unlocked().private:
                # Refuse invalid input before creating a protocol on a fresh
                # root. Established roots validate against the same guarded
                # snapshot that selects the audience below.
                self._validate_publish_request(message, registry_snapshot=self._registry.snapshot(),
                                               catalog=self._channels.read(), activity=activity)
                self._initialize_private_protocol_unlocked()
            return self._publish_initial_cohort_unlocked(bus_lock, message, origin=origin, activity=activity)

    def initialize_private_protocol(self, *, history: Sequence[Message] = ()) -> str:
        """Marker issuer for a NEW, isolated bus root only.

        Existing unmarked logs require an explicit offline rewrite. This issuer
        creates a protocol marker only for an empty root.
        """
        self.log.path.parent.mkdir(parents=True, exist_ok=True)
        with self.log.locked():
            return self._initialize_private_protocol_unlocked(history=history)

    def _initialize_private_protocol_unlocked(self, *, history: Sequence[Message] = ()) -> str:
        self.log.require_fresh_private_root_unlocked()
        # Selected public history precedes this root's first writable sequence. It is
        # display content, never a copied delivery or permission to run old input.
        previous = 0
        for message in history:
            if message.seq <= previous:
                raise ValueError("Initial history must have ascending unique sequences")
            previous = message.seq
            self.bus.append(lambda seq: PublicRecord.of(replace(message.history_snapshot, seq=seq)))

        # The public marker must never advertise a root whose runtime cannot wake.
        with Coordination(str(CoordinationStore.path_in(self.log.path.parent))) as store:
            store.install_private_runtime()
        root_id = Id128Text.mint()

        # Total order: caller's wire lock, bus lock, registry lock. The
        # durable PENDING guard precedes marker visibility; a failed
        # marker/directory fsync cannot leave a usable registry witness.
        if self._registry.store.path.parent != self.log.path.parent:
            raise RelationViolationError("Private registry must share the bus root")
        with _store_lock(self._registry.store.path):
            guard = PrivateRegistryGuard(self._registry.store.path, root_id)
            guard.create_pending()
            self.log.write_metadata_unlocked(
                WireMetadata.private_root(
                    root_id, claims=True, last_seq=previous, claimed_after_seq=previous,
                )
            )
            install_private_bus_checkpoint(self.log, _bus_locked=True)
            guard.commit_initial()
            return root_id

    def publish_initial_cohort(self, message: Message, *, origin: InitialOrigin) -> Message:
        """Commit public envelope and FULL N private decisions in the SAME fsynced row.

        This private path assumes cooperating Comms writers hold the global
        wire lock. It never publishes from a caller-supplied audience or claim.
        """
        with self.opened_activity() as activity, self.log.locked() as bus_lock:
            return self._publish_initial_cohort_unlocked(bus_lock, message, origin=origin, activity=activity)

    def _publish_initial_cohort_unlocked(
        self, bus_lock: WireStoreLock, message: Message, *, origin: InitialOrigin, activity: ActivityClocks
    ) -> Message:
        """An ordinary initial cohort under the held bus lock."""
        with ExitStack() as resources:
            _require_no_private_owner_rename(self.log.path.parent)
            metadata = self.log.private_marker_unlocked()
            metadata.access.require_append()

            source = resources.enter_context(bus_lock.current_source(self.log, metadata))
            source.require_marker(metadata)
            # The registry and catalog writers' own locks hold one revision
            # from this decision through its append.
            catalog = resources.enter_context(self._channels.reading(self._registry.store))
            snapshot = self._registry.snapshot()
            sender, target, require_channel = self._validate_publish_request(
                message, registry_snapshot=snapshot, catalog=catalog, activity=activity)
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
            def build(seq: int) -> PrivateRecord:
                stored = self._prepare_message_unlocked(
                    message, sender=sender, target=target, sequence=seq,
                    snapshot=snapshot, original_source=source,
                )
                origin.require_source(source, sender, stored.message_id)
                revision = hashlib.sha256(
                    repr((
                        FileRevision.capture(self._registry.store.path),
                        self._channels.revision(),
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
                # Register the frozen subscribers before publishing; inboxes never
                # invent participant identities from a committed message.
                with Coordination(str(CoordinationStore.path_in(self.log.path.parent))) as store:
                    sender_thread.role.register_sender(store.participants, sender_lookup, sender)
                    for thread, lookup in zip(selected, lookups, strict=True):
                        store.participants.register(lookup, thread.name, thread.name, committed=True)
                return PrivateRecord.of(
                    stored, audience=audience, decisions=decisions,
                    control=ControlClassification.ORDINARY.value,
                )

            return origin.append(self.bus, build)


class ResponseWrites(PrivateWrites):
    """Publishes keyed responses."""

    def _publish_keyed_response_unlocked(
        self,
        intent: PublicationIntents,
        *,
        conversation,
        registry_snapshot: RegistrySnapshot | None = None,
        activity: ActivityClocks,
    ) -> Message:
        """Internal append with bus lock; a supplied registry snapshot stays locked."""

        matched, previous_sequence, metadata = self.log.keyed_publication_unlocked(intent)
        if matched is not None:
            return matched
        message = intent.message
        # A durable exact replay above wins even after registry/route renames.
        sender, target, require_channel = self._validate_publish_request(
            message, registry_snapshot=registry_snapshot, catalog=self._channels.read(), activity=activity
        )
        role = (
            self._registry.require(sender).role
            if registry_snapshot is None
            else registry_snapshot.threads[sender].role
        )
        role.require_executable(RelationViolationError("Keyed response sender must be executable."))
        if intent.publication_key != canonical_publication_key(intent.execution_id, target.name):
            raise RelationViolationError("Response publication key does not match its route.")
        def build(seq: int):
            stored = self._prepare_message_unlocked(
                message, sender=sender, target=target, sequence=seq, snapshot=registry_snapshot,
            )
            if not intent.matches_publication(stored):
                raise RelationViolationError("Stored response does not match publication intent.")
            return conversation.record(seq, stored, intent)

        return self.bus.append(build)


class OwnerPublisher(InitialWrites, ClaimWrites):
    """An owner's bus: ordinary and initial sends, protocol issue and claims."""


class ParticipantPublisher(ResponseWrites, ClaimWrites):
    """A selected participant's bus: its keyed responses and its claims."""
