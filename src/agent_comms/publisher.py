"""Publication decisions over one canonical WireLog and registered audience."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    canonical_publication_key,
)

from .bus_publication import (
    PRIVATE_WIRE_FIELD,
    CommittedDelivery,
    HumanOrigin,
    initial_sideband,
    stable_thread_lookup,
)
from .channel_targets import BuiltinChannel, is_channel_target
from .envelope_claim_transitions import (
    ClaimRelease,
    ClaimTransition,
    FileClaimPath,
    WakeAdmission,
    _release_resource,
    apply_transition,
)
from .errors import (
    ClaimEnvelopeUnknownError,
    HumanAdmissionBlockedError,
    HumanInitialUnknownError,
    RelationViolationError,
    UnregisteredThreadError,
)
from .mentions import ThreadMention
from .messages import Message
from .private_registry_guard import _require_no_private_owner_rename
from .registry_document import RegistrySnapshot
from .store_files import (
    StoreLock,
    _store_lock,
    file_revision,
)

if TYPE_CHECKING:

    from .registration import Registration
    from .private_bus_checkpoint import CertifiedSourceRead

from .catalog_store import ChannelCatalog
from .wire_log import WireLog
from .wire_metadata import WireMetadata


class Publisher:
    def __init__(
        self,
        log: WireLog,
        registry: Registration,
        channels: ChannelCatalog,
        *,
        private_response_writes: bool = False,
        private_initial_writes: bool = False,
        private_claim_writes: bool = False,
    ):
        self.log = log
        self._registry = registry
        self._channels = channels
        self._private_response_writes = private_response_writes
        self._private_initial_writes = private_initial_writes
        self._private_claim_writes = private_claim_writes

    def _validate_publish_request(
        self, message: Message, *, registry_snapshot: RegistrySnapshot | None = None
    ) -> tuple[str, str]:
        """Resolve the public route, optionally using a locked registry revision."""

        def canonical(name: str) -> str:
            if registry_snapshot is None:
                return self._registry.canonical_name(name)
            return registry_snapshot.aliases.get(name, name)

        def exists(name: str) -> bool:
            if registry_snapshot is None:
                return name in self._registry
            return canonical(name) in registry_snapshot.threads

        if not exists(message.sender):
            raise UnregisteredThreadError(f"Sender {message.sender!r} is not a registered thread.")
        if BuiltinChannel.aggregate_target(message.target) or (
            is_channel_target(message.target)
            and self._channels.read().is_view_target(message.target)
        ):
            raise RelationViolationError(
                f"View {message.target!r} is a projection, not a routable target."
            )
        if not is_channel_target(message.target) and not exists(message.target):
            raise UnregisteredThreadError(f"Target {message.target!r} is not a registered thread.")
        sender = canonical(message.sender)
        target = message.target
        if not is_channel_target(target) and canonical(target) == sender:
            raise RelationViolationError(f"Thread {sender!r} cannot message itself.")
        return sender, target

    def _prepare_message_unlocked(
        self,
        message: Message,
        *,
        sender: str,
        target: str,
        sequence: int,
        snapshot: RegistrySnapshot | None = None,
        original_source: CertifiedSourceRead | None = None,
    ) -> Message:
        snapshot = snapshot or self._registry.snapshot()
        if original_source is not None:
            original_source.require_marker(self.log._private_marker_unlocked())
        message.require_task_publication(sender, snapshot, original_source)

        def resolve_mention(name: str) -> str | None:
            canonical = snapshot.aliases.get(name, name)
            thread = snapshot.threads.get(canonical)
            if (
                thread is not None
                and thread.role.executable
                and snapshot.statuses[canonical].visible
            ):
                return canonical
            return None

        return replace(
            message,
            sender=sender,
            target=target,
            seq=sequence,
            sender_role=snapshot.threads[sender].role,
            mentions=ThreadMention.find(message.body, resolve_mention),
        )

    def publish_ordinary(
        self, message: Message, *, _human_origin: HumanOrigin | None = None
    ) -> Message:
        """Publish through the canonical audience/decision writer.

        A truly fresh root is initialized once under the bus lock. Existing
        unmarked data requires the explicit archival cutover; a send never
        rewrites history or falls back to an uncoordinated append.
        """
        from .active_route import guard_original_root_write

        if _human_origin is not None and type(_human_origin) is not HumanOrigin:
            raise RelationViolationError("Human origin must be a typed local USER identity.")
        with guard_original_root_write(self.log.path.parent), self.log.locked() as bus_lock:
            self._validate_publish_request(message)
            if not self.log.read_metadata_unlocked().private:
                self._initialize_private_protocol_unlocked()
            return self.publish_initial_cohort(
                message, _bus_lock=bus_lock, _human_origin=_human_origin
            )

    def initialize_private_protocol(self) -> str:
        """Marker issuer for a NEW, isolated bus root only.

        Existing unmarked logs require an explicit offline rewrite. This issuer
        creates a protocol marker only for an empty root.
        """
        if self._private_initial_writes is not True:
            raise RelationViolationError("Private initial publication is disabled.")
        self.log.path.parent.mkdir(parents=True, exist_ok=True)
        with self.log.locked():
            return self._initialize_private_protocol_unlocked()

    def _initialize_private_protocol_unlocked(self) -> str:
        if self._private_initial_writes is not True:
            raise RelationViolationError("Private initial publication is disabled.")
        self.log.require_fresh_private_root_unlocked()
        from agent_comms.coordinator import Coordination

        # The public marker must never advertise a root whose runtime cannot wake.
        with Coordination(str(self.log.path.parent / "coordination.sqlite3")) as store:
            store.install_private_runtime()
        root_id = uuid.uuid4().hex
        from .private_bus_checkpoint import install_private_bus_checkpoint
        from .private_registry_guard import PrivateRegistryGuard

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

    def publish_claim_envelope(
        self,
        message: Message,
        *,
        worktree: Path,
        incarnation: str,
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path] = (),
        _locked_registry_snapshot: RegistrySnapshot | None = None,
        _bus_locked: bool = False,
        _admission: WakeAdmission | None = None,
    ) -> Message:
        """One guarded message and whole-set claim transition in ONE bus row.

        Caller must hold the global Comms wire lock; the bus lock serializes all
        cooperating claim decisions. Failed durability returns UNKNOWN: a later
        guarded reader may re-fsync/adopt a complete visible row, but the caller
        MUST NOT replay its message or provider/tool work automatically.
        """
        from .audience_manifest import MAX_WIRE_SEQ

        if not self._private_claim_writes:
            raise RelationViolationError("Claim envelope publication is disabled.")
        if message.claim_transition is not None or (not claims and not releases):
            raise RelationViolationError("A claim send needs exactly one fresh transition.")
        if (
            isinstance(claims, (str, bytes))
            or isinstance(releases, (str, bytes))
            or not isinstance(claims, Sequence)
            or not isinstance(releases, Sequence)
        ):
            raise RelationViolationError("Claim and release sets must be finite sequences.")
        if len(claims) + len(releases) > 32:
            raise RelationViolationError("Claim envelope exceeds the bounded resource set.")
        if _admission is not None and (
            not claims or releases or not _bus_locked or _locked_registry_snapshot is None
        ):
            raise RelationViolationError("Bound claims require the selected wake boundary.")
        with nullcontext() if _bus_locked else self.log.locked():
            metadata = self.log._private_marker_unlocked()
            if not metadata.claims:
                raise RelationViolationError("Claim read barrier is unavailable.")
            snapshot = (
                self._registry.snapshot()
                if _locked_registry_snapshot is None
                else _locked_registry_snapshot
            )
            snapshot.require_unambiguous_ownership()
            sender, target = self._validate_publish_request(message, registry_snapshot=snapshot)
            projection, verified_sequence = self.log._claim_projection_unlocked(metadata)
            # The verified bus high-water also covers rows left by an earlier
            # uncertain append. Reserve and sync the next sequence before use.
            last_sequence = max(metadata.last_seq, verified_sequence)
            if last_sequence >= MAX_WIRE_SEQ:
                raise RelationViolationError("Claim bus sequence is exhausted.")
            stored = self._prepare_message_unlocked(
                message,
                sender=sender,
                target=target,
                sequence=last_sequence + 1,
                snapshot=snapshot,
            )
            owner_incarnation = str(incarnation)
            requested = tuple(sorted(path.normalized(worktree) for path in claims))
            release_paths = tuple(sorted(_release_resource(worktree, path) for path in releases))
            release_records: list[ClaimRelease] = []
            for resource in release_paths:
                current = projection.get(resource)
                if current is None:
                    raise RelationViolationError("Cannot release an unclaimed resource.")
                release_records.append(ClaimRelease(resource, current.generation))
            transition = ClaimTransition(
                stored.sender,
                owner_incarnation,
                stored.seq,
                stored.message_id,
                requested,
                tuple(release_records),
                uuid.uuid4().hex if requested else None,
                _admission,
            )
            apply_transition(projection, transition)  # pre-append conflict is synchronous
            stored = replace(stored, claim_transition=transition)
            try:
                self.log._append_private_unlocked(metadata, stored.to_wire())
            except (OSError, RelationViolationError) as error:
                raise ClaimEnvelopeUnknownError(
                    "Claim envelope outcome UNKNOWN; inspect durable bus; do not replay."
                ) from error
            return stored

    def publish_initial_cohort(
        self,
        message: Message,
        *,
        control: str = "ordinary",
        _bus_lock: StoreLock | None = None,
        _human_origin: HumanOrigin | None = None,
    ) -> Message:
        """Commit public envelope and FULL N private decisions in the SAME fsynced row.

        This private path assumes cooperating Comms writers hold the global
        wire lock. It never publishes from a caller-supplied audience or claim.
        """
        from .audience_manifest import MAX_WIRE_SEQ, FrozenRecipient, freeze_audience
        from .wake import ControlClassification, resolve_wake_cohort

        if self._private_initial_writes is not True:
            raise RelationViolationError("Private initial publication is disabled.")
        if _human_origin is not None and type(_human_origin) is not HumanOrigin:
            raise RelationViolationError("Human initial requires a typed local USER origin.")
        if message.claim_transition is not None:
            raise RelationViolationError("An initial cohort cannot carry resource claims.")
        classification = ControlClassification(control)
        if not classification.supports_initial:
            raise RelationViolationError("System-control initial issuer is not available.")
        with nullcontext(_bus_lock) if _bus_lock is not None else self.log.locked() as bus_lock:
            _require_no_private_owner_rename(self.log.path.parent)
            metadata = self.log._private_marker_unlocked()
            metadata.access.require_append()
            from .private_bus_checkpoint import verify_private_bus_checkpoint_unlocked

            previous_sequence = verify_private_bus_checkpoint_unlocked(
                self.log, metadata
            ).through_seq
            if metadata.last_seq >= MAX_WIRE_SEQ:
                raise RelationViolationError("Private bus sequence is exhausted.")
            source_paths = (
                self._registry.store.path,
                self._channels.path,
            )
            before_revisions = tuple(file_revision(path) for path in source_paths)
            snapshot = self._registry.snapshot()
            snapshot.require_unambiguous_ownership()
            sender = snapshot.aliases.get(message.sender, message.sender)
            sender_thread = snapshot.threads.get(sender)
            if sender_thread is None or not snapshot.statuses[sender].visible:
                raise RelationViolationError("Initial sender must be visible and registered.")
            if _human_origin is None:
                if not sender_thread.role.executable:
                    raise RelationViolationError(
                        "Initial sender must be a visible registered executable."
                    )
            elif (
                sender_thread.role.executable
                or message.sender != sender_thread.name
                or _human_origin.sender != sender_thread.name
                or _human_origin.created_at != sender_thread.created_at
                or _human_origin.worktree != sender_thread.worktree
            ):
                raise RelationViolationError("Local USER origin differs from registered identity.")
            catalog = self._channels.read()
            if BuiltinChannel.aggregate_target(message.target) or catalog.is_view_target(
                message.target
            ):
                raise RelationViolationError("A saved/aggregate view is not routable.")
            target = message.target
            if not is_channel_target(target):
                # Bind the alias in this guarded publication snapshot. The
                # envelope and frozen audience carry its canonical incarnation.
                target = snapshot.aliases.get(target, target)
                recipient = snapshot.threads.get(target)
                if recipient is None or not snapshot.statuses[target].visible:
                    raise RelationViolationError("Initial direct target must be visible.")
                if target == sender:
                    raise RelationViolationError("A thread cannot message itself.")
                names = [target] if recipient.role.executable else []
                if not recipient.role.executable:
                    # A human DM is displayed, never admitted as agent work.
                    message = replace(message, notice=True)
            else:
                channel = catalog.resolve(target)
                names = [
                    name
                    for name, thread in snapshot.threads.items()
                    if name != sender
                    and thread.role.executable
                    and snapshot.statuses[name].visible
                    and channel.matches(thread.tags)
                ]
            selected = [snapshot.threads[name] for name in names]
            lookups = [stable_thread_lookup(thread.created_at) for thread in selected]
            if len(set(lookups)) != len(lookups):
                raise RelationViolationError("Recipient creation identities collide.")
            sender_lookup = stable_thread_lookup(snapshot.threads[sender].created_at)
            if sender_lookup in lookups:
                raise RelationViolationError("Sender creation identity collides with recipient.")
            stored = self._prepare_message_unlocked(
                message,
                sender=sender,
                target=target,
                sequence=max(metadata.last_seq, previous_sequence) + 1,
                snapshot=snapshot,
                original_source=bus_lock.source,
            )
            if _human_origin is not None:
                # The marker reserves a sequence before the row. A crash after
                # reservation can leave NO row carrying the human message ID.
                # Never admit another human input while such an UNKNOWN gap is
                # present, even if another ordinary sender subsequently skips
                # over that sequence. This intentionally favors safety over
                # availability until explicit operator reconciliation exists.
                expected_sequence = 1
                duplicate = False
                for record in self.log.verified_records_unlocked(metadata):
                    previous = record.message
                    if previous.seq != expected_sequence:
                        raise HumanAdmissionBlockedError(
                            "Private bus sequence gap has UNKNOWN outcome; "
                            "human send blocked, do not retry."
                        )
                    expected_sequence += 1
                    duplicate |= (
                        previous.sender == sender and previous.message_id == stored.message_id
                    )
                if metadata.last_seq != expected_sequence - 1:
                    raise HumanAdmissionBlockedError(
                        "Private bus sequence reservation has UNKNOWN outcome; "
                        "human send blocked, do not retry."
                    )
                if duplicate:
                    raise RelationViolationError(
                        "Human initial ID already exists; inspect its receipt, do not retry."
                    )
            revision = hashlib.sha256(
                repr(
                    (
                        file_revision(self._registry.store.path),
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
                stored, frozen_audience=audience, control=classification
            )
            from .delivery_policy import InitialDeliveryPolicy
            from .field_codec import FieldCodec

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
                            control=classification.value,
                        ),
                    )
                ),
            }
            # Check the exact bytes and one coherent source revision before any append.
            CommittedDelivery.from_wire(row, metadata.root_id)
            if before_revisions != tuple(file_revision(path) for path in source_paths):
                raise RelationViolationError("Send-time registry/catalog revision changed.")
            # Every frozen subscriber needs an identity, including stopped
            # observers. Register from this authoritative registry snapshot,
            # before publishing; an inbox must never invent owners from a row.
            from .coordinator import Coordination

            with Coordination(str(self.log.path.parent / "coordination.sqlite3")) as store:
                if sender_thread.role.executable:
                    store.participants.register(sender_lookup, sender, sender, committed=True)
                for thread, lookup in zip(selected, lookups, strict=True):
                    store.participants.register(lookup, thread.name, thread.name, committed=True)
            if _human_origin is None:
                self.log._append_private_unlocked(metadata, row)
            else:
                try:
                    self.log._append_private_unlocked(metadata, row)
                except BaseException as error:
                    # Even cancellation/interrupt after entry can follow a durable
                    # reservation or row. Never claim absence or retry this ID.
                    raise HumanInitialUnknownError(
                        metadata.root_id, stored.seq, stored.message_id
                    ) from error
            return stored

    def _publish_keyed_response_unlocked(
        self,
        intent: PublicationIntents,
        *,
        conversation,
        registry_snapshot: RegistrySnapshot | None = None,
    ) -> Message:
        """Internal append with bus lock; a supplied registry snapshot stays locked."""

        from .audience_manifest import MAX_WIRE_SEQ

        if self._private_response_writes is not True:
            raise RelationViolationError("Private response publication is disabled.")
        matched, previous_sequence, metadata = self.log._keyed_receipt_unlocked(intent)
        if matched is not None:
            return matched
        message = intent.expected_message
        # A durable exact replay above wins even after registry/route renames.
        sender, target = self._validate_publish_request(
            message, registry_snapshot=registry_snapshot
        )
        if registry_snapshot is None:
            executable = self._registry.require(sender).role.executable
        else:
            executable = registry_snapshot.threads[sender].role.executable
        if not executable:
            raise RelationViolationError("Keyed response sender must be executable.")
        if intent.publication_key != canonical_publication_key(intent.execution_id, target):
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
        CommittedDelivery.from_wire(row, metadata.root_id)
        self.log._append_private_unlocked(metadata, row)
        return stored
