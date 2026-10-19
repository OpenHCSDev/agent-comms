"""Publication decisions over one canonical WireLog and registered audience."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from functools import partial

from .bus_activity_index import ActivityClocks
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    canonical_publication_key,
)

from .active_route import guard_original_root_write
from .audience_manifest import MAX_WIRE_SEQ, FrozenRecipient, freeze_audience
from .bus_publication import (
    PRIVATE_WIRE_FIELD,
    InitialOrigin,
    initial_sideband,
)
from .channel_targets import CommandTarget, NamedTarget
from .coordinator import Coordination
from .delivery_policy import InitialDeliveryPolicy
from .envelope_claim_transitions import (
    ClaimRelease,
    ClaimTransition,
    FileClaimPath,
    WakeClaim,
    _release_resource,
)
from .errors import (
    ClaimEnvelopeUnknownError,
    RelationViolationError,
    UnregisteredThreadError,
)
from .field_codec import FieldCodec, Id128Text
from .mentions import ThreadMention
from .messages import Message
from .private_bus_checkpoint import (
    install_private_bus_checkpoint,
    opened_private_checkpoint_unlocked,
)
from .private_path import FileRevision
from .private_registry_guard import PrivateRegistryGuard, _require_no_private_owner_rename
from .registry_document import RegistrySnapshot
from .store_files import StoreLock, _store_lock
from .wake import ControlClassification, resolve_wake_cohort

if TYPE_CHECKING:

    from .private_bus_checkpoint import CertifiedSourceRead
    from .registration import Registration

from .catalog_document import CatalogDocument
from .channels import Channel
from .catalog_store import ChannelCatalog
from .wire_log import WireLog
from .wire_metadata import WireMetadata


class Publisher:
    """Reads and validates publications; each private write is a capability below."""

    def __init__(self, log: WireLog, registry: Registration, channels: ChannelCatalog, opened_activity):
        self.log = log
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
        snapshot: RegistrySnapshot | None = None,
        original_source: CertifiedSourceRead | None = None,
    ) -> Message:
        snapshot = snapshot or self._registry.snapshot()
        if original_source is not None:
            original_source.require_marker(self.log.private_marker_unlocked())
        message.require_task_publication(sender, snapshot, original_source)

        def resolve_mention(name: str) -> str | None:
            canonical = snapshot.canonical_name(name)
            thread = snapshot.threads.get(canonical)
            if thread is None or not snapshot.statuses[canonical].visible:
                return None
            return next(iter(thread.role.executor(canonical)), None)

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

    def publish_claim_envelope(
        self,
        message: Message,
        *,
        worktree: Path,
        incarnation: str,
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path | ClaimRelease] = (),
    ) -> Message:
        """One guarded message and whole-set claim transition in ONE bus row.

        Caller must hold the global Comms wire lock; the bus lock serializes all
        cooperating claim decisions. Failed durability returns UNKNOWN: a later
        guarded reader may re-fsync/adopt a complete visible row, but the caller
        MUST NOT replay its message or provider/tool work automatically.
        """
        with self.opened_activity() as activity, self.log.locked() as bus_lock:
            return self._publish_claim_envelope_unlocked(
                bus_lock, self._registry.snapshot(), message,
                worktree=worktree, incarnation=incarnation, claims=claims, releases=releases, activity=activity,
            )

    def _publish_claim_envelope_unlocked(
        self,
        bus_lock: StoreLock,
        snapshot: RegistrySnapshot,
        message: Message,
        *,
        worktree: Path,
        incarnation: str,
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path | ClaimRelease] = (),
        claim: WakeClaim | None = None,
        activity: ActivityClocks,
    ) -> Message:
        """The claim envelope under a held bus lock and its locked registry snapshot."""
        if message.claim_transition is not None or (not claims and not releases):
            raise RelationViolationError("A claim send needs exactly one fresh transition.")
        if (
            isinstance(claims, (str, bytes))
            or isinstance(releases, (str, bytes))
            or not isinstance(claims, Sequence)
            or not isinstance(releases, Sequence)
        ):
            raise RelationViolationError("Claim and release sets must be finite sequences.")
        if len(claims) + len(releases) > ClaimTransition.max_resources:
            raise RelationViolationError("Claim envelope exceeds the bounded resource set.")
        if claim is not None and (not claims or releases):
            raise RelationViolationError("Bound claims require the selected wake boundary.")
        metadata = self.log.private_marker_unlocked()
        if not metadata.claims:
            raise RelationViolationError("Claim read barrier is unavailable.")
        sender, target, require_channel = self._validate_publish_request(message, registry_snapshot=snapshot, catalog=self._channels.read(), activity=activity)
        with bus_lock.current_source(self.log, metadata) as source:
            # The checkpoint high-water also covers a prior visible append;
            # accepting new input never replays a historical message or claim.
            last_sequence = max(metadata.last_seq, source.committed_sequence())
            if last_sequence >= MAX_WIRE_SEQ:
                raise RelationViolationError("Claim bus sequence is exhausted.")
            stored = self._prepare_message_unlocked(
                message, sender=sender, target=target,
                sequence=last_sequence + 1, snapshot=snapshot,
            )
            owner_incarnation = str(incarnation)
            requested = tuple(sorted(path.normalized(worktree) for path in claims))
            release_records: list[ClaimRelease] = []
            for release in releases:
                if isinstance(release, ClaimRelease):
                    # Recorded incarnation/generation, not current project,
                    # determines whether this exact resource may be released.
                    release_records.append(release)
                else:
                    resource = _release_resource(worktree, release)
                    current = source.claim(resource)
                    if current is None:
                        raise RelationViolationError("Cannot release an unclaimed resource.")
                    release_records.append(ClaimRelease(resource, current.generation))
            release_records.sort(key=lambda release: release.resource)
            transition = ClaimTransition(
                stored.sender, owner_incarnation, stored.seq, stored.message_id,
                requested, tuple(release_records),
                Id128Text.mint() if requested else None, claim,
            )
            source.prepare_claim(transition)  # pre-append conflict is synchronous
        stored = replace(stored, claim_transition=transition)
        try:
            self.log.append_private_unlocked(metadata, stored.to_wire())
        except (OSError, RelationViolationError) as error:
            raise ClaimEnvelopeUnknownError(
                "Claim envelope outcome UNKNOWN; inspect durable bus; do not replay."
            ) from error
        return stored


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

    def initialize_private_protocol(self) -> str:
        """Marker issuer for a NEW, isolated bus root only.

        Existing unmarked logs require an explicit offline rewrite. This issuer
        creates a protocol marker only for an empty root.
        """
        self.log.path.parent.mkdir(parents=True, exist_ok=True)
        with self.log.locked():
            return self._initialize_private_protocol_unlocked()

    def _initialize_private_protocol_unlocked(self) -> str:
        self.log.require_fresh_private_root_unlocked()

        # The public marker must never advertise a root whose runtime cannot wake.
        with Coordination(str(self.log.path.parent / "coordination.sqlite3")) as store:
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
                WireMetadata(
                    last_seq=0,
                    writer_protocol_version=1,
                    wire_root_id=root_id,
                    claim_envelopes_version=1,
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
        self, bus_lock: StoreLock, message: Message, *, origin: InitialOrigin, activity: ActivityClocks
    ) -> Message:
        """An ordinary initial cohort under the held bus lock."""
        if message.claim_transition is not None:
            raise RelationViolationError("An initial cohort cannot carry resource claims.")
        with ExitStack() as resources:
            _require_no_private_owner_rename(self.log.path.parent)
            metadata = self.log.private_marker_unlocked()
            metadata.access.require_append()

            # Fresh initialization occurs after lock acquisition and has no
            # acquired source. Existing roots already borrowed this exact cut
            # from the lock's durability owner; keep using it until append.
            source = bus_lock.source
            if source is None:
                source = resources.enter_context(
                    opened_private_checkpoint_unlocked(self.log, metadata)
                )
            source.require_marker(metadata)
            previous_sequence = source.committed_sequence()
            if metadata.last_seq >= MAX_WIRE_SEQ:
                raise RelationViolationError("Private bus sequence is exhausted.")
            source_paths = (
                self._registry.store.path,
                self._channels.path,
            )
            before_revisions = tuple(FileRevision.capture(path) for path in source_paths)
            snapshot = self._registry.snapshot()
            catalog = self._channels.read()
            sender, target, require_channel = self._validate_publish_request(
                message, registry_snapshot=snapshot, catalog=catalog, activity=activity)
            sender_thread = snapshot.threads.get(sender)
            if sender_thread is None or not snapshot.statuses[sender].visible:
                raise RelationViolationError("Initial sender must be visible and registered.")
            # The kind owns its own validation; one publication check, before
            # any sequence reservation, refusing with what is missing.
            message.type.require_publishable(message, Path(sender_thread.worktree))
            origin.require_sender(message, sender_thread, snapshot)
            target, message, names = target.initial_delivery(
                sender, message, snapshot, require_channel)
            selected = [snapshot.threads[name] for name in names]
            lookups = [thread.incarnation.lookup for thread in selected]
            sender_lookup = snapshot.threads[sender].incarnation.lookup
            stored = self._prepare_message_unlocked(
                message,
                sender=sender,
                target=target,
                sequence=max(metadata.last_seq, previous_sequence) + 1,
                snapshot=snapshot,
                original_source=source,
            )
            origin.require_source(source, sender, stored.message_id)
            revision = hashlib.sha256(
                repr(
                    (
                        FileRevision.capture(self._registry.store.path),
                        self._channels.revision(),
                        sorted(
                            (name, thread.created_at, sorted(thread.tags))
                            for name, thread in snapshot.threads.items()
                        ),
                    )
                ).encode()
            ).hexdigest()
            audience = freeze_audience(
                stored,
                tuple(
                    FrozenRecipient(lookup, name)
                    for lookup, name in zip(lookups, names, strict=True)
                ),
                revision,
                sender_lookup=sender_lookup,
                sender_name=sender,
            )
            decisions = resolve_wake_cohort(
                stored, frozen_audience=audience, control=ControlClassification.ORDINARY
            )
            row = {
                **stored.to_wire(),
                PRIVATE_WIRE_FIELD: FieldCodec.encode(
                    InitialDeliveryPolicy(
                        version=1,
                        initial=initial_sideband(
                            metadata.root_id,
                            stored,
                            audience,
                            decisions,
                            control=ControlClassification.ORDINARY.value,
                        ),
                    )
                ),
            }
            # One coherent source revision before any append; the append attests the row.
            if before_revisions != tuple(FileRevision.capture(path) for path in source_paths):
                raise RelationViolationError("Send-time registry/catalog revision changed.")
            # Every frozen subscriber needs an identity, including stopped
            # observers. Register from this authoritative registry snapshot,
            # before publishing; an inbox must never invent owners from a row.
            with Coordination(str(self.log.path.parent / "coordination.sqlite3")) as store:
                sender_thread.role.register_sender(store.participants, sender_lookup, sender)
                for thread, lookup in zip(selected, lookups, strict=True):
                    store.participants.register(lookup, thread.name, thread.name, committed=True)
            origin.append(self.log, metadata, row, stored)
            return stored


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
        last_sequence = max(metadata.last_seq, previous_sequence)
        if last_sequence >= MAX_WIRE_SEQ:
            raise RelationViolationError("Private bus sequence is exhausted.")
        stored = self._prepare_message_unlocked(
            message,
            sender=sender,
            target=target,
            sequence=last_sequence + 1,
            snapshot=registry_snapshot,
        )
        if not intent.matches_publication(stored):
            raise RelationViolationError("Stored response does not match publication intent.")
        row = conversation.record(metadata.root_id, stored, intent)
        self.log.append_private_unlocked(metadata, row)
        return stored


class OwnerPublisher(InitialWrites, ClaimWrites):
    """An owner's bus: ordinary and initial sends, protocol issue and claims."""


class ParticipantPublisher(ResponseWrites, ClaimWrites):
    """A selected participant's bus: its keyed responses and its claims."""
