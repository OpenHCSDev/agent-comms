"""Declared cohort evidences and optional acceptance-generation provenance."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.coordination_errors import IdentityConflict, SchemaVersionError
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.participants import OwnerGenerations, OwnerRecord, Participants
from agent_comms.private_runtime_schema import PrivateCoordinatorTable

from .audience_manifest import MAX_AUDIENCE_BYTES
from .coordination_contracts import POLICY_VERSION, RESOLVER_VERSION
from .field_codec import AssignmentId, MalformedRecord, MessageId, RootId, SubclassUnion, ThreadLookup, WireName
from .thread_text import AbsentText, GivenText, TextChoice
from .response_acknowledgement import _canonical
from .typed_table import (
    Column,
    ColumnGuard,
    DurableTables,
    ForeignKey,
    FrozenColumn,
    GuardedColumns,
    GuardedInserts,
    GuardedRelatedInserts,
    ImmutableRows,
    Index,
    InitialOneWayColumn,
    NoDelete,
    SharedFileTables,
    TextStorage,
    TypedTable,
    WithoutRowid,
)
from .wake_candidate_index import ProjectionUnavailableError

if TYPE_CHECKING:
    from .bus_publication import Delivered
    from .private_registry_guard import RegistryPrivacy

_LOG = logging.getLogger(__name__)


class CohortTable(PrivateCoordinatorTable):
    """Mandatory sealed N/K acknowledgement declarations."""

    __slots__ = ()


class AwarenessTable(SharedFileTables, DurableTables):
    """Optional provenance captured only during the unsealed acceptance transaction."""

    __slots__ = ()
    schema_error = SchemaVersionError

    @classmethod
    def install_optional(cls, db: sqlite3.Connection) -> None:
        """Optional provenance cannot roll back mandatory native preparation."""
        db.execute("SAVEPOINT optional_awareness_install")
        try:
            if not cls.stored_schema(db):
                cls.create_schema(db)
            assert_optional_awareness_schema(db)
        except (sqlite3.Error, SchemaVersionError, MalformedRecord) as error:
            db.execute("ROLLBACK TO optional_awareness_install")
            _LOG.warning("Optional awareness schema unavailable: %r", error, exc_info=error)
        finally:
            db.execute("RELEASE optional_awareness_install")


@dataclass(frozen=True, kw_only=True)
class ClaimBatchAcknowledgements(
    InitialOneWayColumn,
    GuardedColumns,
    NoDelete,
    CohortTable,
    WithoutRowid,
    declared_name="claim_batch_receipts",
):
    transition_column = "sealed"
    transition_from = 0
    transition_to = 1
    column_guards = (
        ColumnGuard(('sealed',), """(((SELECT COUNT(*) FROM cohort_delivery_receipts d
        WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq)
        != OLD.member_count OR (SELECT COUNT(*) FROM cohort_delivery_receipts d
        WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq
        AND d.claim_id IS NOT NULL) != OLD.claim_count)
        OR ((SELECT COUNT(*) FROM claim_batch_members m
        WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq)
        != OLD.claim_count) OR (EXISTS (SELECT 1 FROM cohort_delivery_receipts d
        WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq
        AND d.ordinal >= OLD.member_count) OR EXISTS (SELECT 1 FROM claim_batch_members m
        WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq
        AND m.ordinal >= OLD.claim_count)) OR (EXISTS (SELECT 1 FROM claim_batch_members m
        LEFT JOIN cohort_delivery_receipts d ON d.wire_root_id = m.wire_root_id
        AND d.wire_seq = m.wire_seq AND d.claim_id = m.claim_id
        WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq
        AND (d.claim_id IS NULL OR m.ordinal != (
        SELECT COUNT(*) FROM cohort_delivery_receipts preceding
        WHERE preceding.wire_root_id = OLD.wire_root_id AND preceding.wire_seq = OLD.wire_seq
        AND preceding.claim_id IS NOT NULL AND preceding.ordinal < d.ordinal))))
        OR (EXISTS (SELECT 1 FROM cohort_delivery_receipts d
        JOIN wake_claims c ON c.recipient_lookup = d.recipient_lookup AND c.wire_seq = d.wire_seq
        WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq
        AND d.claim_id IS NULL)))"""),
    )
    manifest_format: ClassVar[str] = "audience-v1"
    decision_domain: ClassVar[bytes] = b"agent-comms:cohort-decisions:v1\0"

    wire_root_id: Annotated[
        RootId,
        FrozenColumn(
            primary_key=True,
            check=(
                "\n"
                "                length(wire_root_id) = 32 AND wire_root_id NOT G"
                "LOB '*[^0-9a-f]*'"
            ),
        ),
    ]
    wire_seq: Annotated[int, FrozenColumn(primary_key=True, check="wire_seq > 0")]
    message_id: Annotated[MessageId, FrozenColumn(check="length(message_id) BETWEEN 1 AND 256")]
    exact_target: Annotated[str, FrozenColumn(check="length(exact_target) BETWEEN 1 AND 256")]
    envelope_digest: Annotated[str, FrozenColumn(check="length(envelope_digest) = 64")]
    audience_digest: Annotated[str, FrozenColumn(check="length(audience_digest) = 64")]
    decisions_digest: Annotated[str, FrozenColumn(check="length(decisions_digest) = 64")]
    member_count: Annotated[int, FrozenColumn(check="member_count BETWEEN 0 AND 4096")]
    claim_count: Annotated[int, FrozenColumn(check="claim_count BETWEEN 0 AND member_count")]
    manifest_codec: Annotated[str, FrozenColumn(check="length(manifest_codec) BETWEEN 1 AND 256")]
    resolver_version: Annotated[
        str, FrozenColumn(check="length(resolver_version) BETWEEN 1 AND 256")
    ]
    policy_version: Annotated[str, FrozenColumn(check="length(policy_version) BETWEEN 1 AND 256")]
    accepted_at_ms: Annotated[int, FrozenColumn(check="accepted_at_ms >= 0")]
    sealed: bool = False

    @classmethod
    def decision_hash(cls, delivery: Delivered) -> str:
        encoded = _canonical([decision.to_wire() for decision in delivery.decisions])
        if len(encoded) > MAX_AUDIENCE_BYTES:
            raise ValueError("Private decision set exceeds its byte bound.")
        return hashlib.sha256(cls.decision_domain + encoded).hexdigest()

    @classmethod
    def for_delivery(
        cls, delivery: Delivered, privacy: RegistryPrivacy, accepted_at_ms: int,
    ) -> ClaimBatchAcknowledgements:
        """Native acknowledgement facts derive from the actual delivered value."""
        return cls(
            wire_root_id=privacy.namespace,
            wire_seq=delivery.seq,
            message_id=delivery.message_id,
            exact_target=delivery.target,
            envelope_digest=delivery.wire_envelope_digest,
            audience_digest=delivery.digest,
            decisions_digest=cls.decision_hash(delivery),
            member_count=len(delivery.recipients),
            claim_count=sum(len(decision.claim_decisions()) for decision in delivery.decisions),
            manifest_codec=cls.manifest_format,
            resolver_version=RESOLVER_VERSION,
            policy_version=POLICY_VERSION,
            accepted_at_ms=accepted_at_ms,
        )

    def require_delivery(self, delivery: Delivered, privacy: RegistryPrivacy) -> None:
        if replace(self, sealed=False) != self.for_delivery(delivery, privacy, self.accepted_at_ms):
            raise IdentityConflict("sealed cohort acknowledgement conflicts with committed bus")

    def require_selected_claim(self, assignment: WakeAssignment) -> None:
        """An awareness contribution borrows this original sealed acknowledgement."""
        if not self.sealed or self.message_id != assignment.message_id:
            raise ProjectionUnavailableError("selected source acknowledgement is unsealed or changed")

@dataclass(frozen=True, kw_only=True)
class ClaimBatchMembers(GuardedInserts, ImmutableRows, CohortTable, WithoutRowid):

    insert_condition = """((SELECT sealed FROM claim_batch_receipts r
        WHERE r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq) != 0)
        OR (NOT EXISTS (SELECT 1 FROM wake_claims c JOIN claim_batch_receipts r
        ON r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq
        WHERE c.assignment_id = NEW.claim_id AND c.recipient_lookup = NEW.recipient_lookup
        AND c.wire_seq = NEW.wire_seq AND c.message_id = r.message_id
        AND c.resolver_version = r.resolver_version AND c.policy_version = r.policy_version
        AND c.accepted_at_ms = r.accepted_at_ms))"""

    wire_root_id: Annotated[RootId, Column(primary_key=True)]
    wire_seq: Annotated[int, Column(primary_key=True)]
    ordinal: Annotated[int, Column(primary_key=True, check="ordinal >= 0")]
    claim_id: AssignmentId
    recipient_lookup: ThreadLookup
    unique = (
        ("wire_root_id", "wire_seq", "claim_id"),
        ("claim_id",),
        ("wire_root_id", "wire_seq", "recipient_lookup"),
    )

    @classmethod
    def references(cls):
        return (
            ForeignKey(columns=("claim_id",), target=WakeAssignment, target_columns=("assignment_id",)),
            ForeignKey(columns=("recipient_lookup",), target=Participants, target_columns=("participant_lookup",)),
            ForeignKey(
                columns=("wire_root_id", "wire_seq"), target=ClaimBatchAcknowledgements, target_columns=("wire_root_id", "wire_seq")
            ),
        )

class DeliveryClaim(SubclassUnion, TextChoice, affix="Claim"):
    """A delivery's selected assignment, or no claim for an observer."""


@dataclass(frozen=True, slots=True)
class SelectedClaim(GivenText, DeliveryClaim):
    text: AssignmentId

    def __post_init__(self) -> None:
        AssignmentId.spelled_by.from_text(self.text)


@dataclass(frozen=True, slots=True)
class NoDeliveryClaim(AbsentText, DeliveryClaim):
    pass


@dataclass(frozen=True, kw_only=True)
class CohortDeliveries(
    GuardedRelatedInserts,
    GuardedInserts,
    ImmutableRows,
    CohortTable,
    WithoutRowid,
    declared_name="cohort_delivery_receipts",
):
    """Sealed deliveries, including observers that receive no execution claim."""

    related_insert_target = WakeAssignment
    related_insert_condition = """EXISTS (SELECT 1 FROM cohort_delivery_receipts d
        JOIN claim_batch_receipts r ON r.wire_root_id=d.wire_root_id AND r.wire_seq=d.wire_seq
        AND r.sealed=1 WHERE d.recipient_lookup=NEW.recipient_lookup AND d.wire_seq=NEW.wire_seq
        AND d.claim_id IS NULL)"""
    insert_condition = """((SELECT sealed FROM claim_batch_receipts r
        WHERE r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq) != 0)
        OR (NEW.claim_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM claim_batch_members m
        JOIN wake_claims c ON c.assignment_id = m.claim_id WHERE m.wire_root_id = NEW.wire_root_id
        AND m.wire_seq = NEW.wire_seq AND m.claim_id = NEW.claim_id
        AND m.recipient_lookup = NEW.recipient_lookup AND c.recipient = NEW.canonical_thread))"""

    wire_root_id: Annotated[RootId, Column(primary_key=True)]
    wire_seq: Annotated[int, Column(primary_key=True)]
    ordinal: Annotated[int, Column(primary_key=True, check="ordinal >= 0")]
    recipient_lookup: ThreadLookup
    canonical_thread: Annotated[
        str, Column(check="length(canonical_thread) BETWEEN 1 AND 256")
    ]
    claim_id: Annotated[DeliveryClaim, Column(storage=TextStorage, nullable=True)]
    unique = (
        ("wire_root_id", "wire_seq", "recipient_lookup"),
        ("wire_root_id", "wire_seq", "canonical_thread"),
        ("wire_root_id", "wire_seq", "claim_id"),
    )
    indexes = (Index(("recipient_lookup", "wire_seq")),)

    @classmethod
    def sealed_sequences_after(
        cls,
        db: sqlite3.Connection,
        *,
        wire_root_id: RootId,
        recipient_lookup: ThreadLookup,
        after_seq: int,
        limit: int,
    ) -> frozenset[int]:
        """Sealed positions for one bounded page after its acceptance frontier."""
        rows = db.execute(
            "SELECT d.wire_seq FROM cohort_delivery_receipts d "
            "JOIN claim_batch_receipts r ON r.wire_root_id=d.wire_root_id "
            "AND r.wire_seq=d.wire_seq AND r.sealed=1 "
            "WHERE d.wire_root_id=? AND d.recipient_lookup=? AND d.wire_seq>? "
            "ORDER BY d.wire_seq LIMIT ?",
            (wire_root_id, recipient_lookup, after_seq, limit),
        )
        return frozenset(row[0] for row in rows)

    @classmethod
    def references(cls):
        return (
            ForeignKey(columns=("recipient_lookup",), target=Participants, target_columns=("participant_lookup",)),
            ForeignKey(columns=("claim_id",), target=WakeAssignment, target_columns=("assignment_id",)),
            ForeignKey(
                columns=("wire_root_id", "wire_seq"), target=ClaimBatchAcknowledgements, target_columns=("wire_root_id", "wire_seq")
            ),
        )


@dataclass(frozen=True, kw_only=True)
class AcceptanceFrontier(CohortTable, WithoutRowid):
    """Addressed originals visited for acceptance, independent of native consumption."""

    page: ClassVar[int] = 101

    wire_root_id: Annotated[RootId, Column(primary_key=True)]
    recipient_lookup: Annotated[ThreadLookup, Column(primary_key=True)]
    through_seq: Annotated[int, Column(check="through_seq >= 0")]

    @classmethod
    def scan_after(
        cls,
        db: sqlite3.Connection,
        *,
        wire_root_id: RootId,
        recipient_lookup: ThreadLookup,
    ) -> int:
        return cls.require(
            db, wire_root_id=wire_root_id, recipient_lookup=recipient_lookup,
        ).through_seq

    def advance(self, db: sqlite3.Connection) -> None:
        """A concurrent acceptance can extend this scan, never rewind it."""
        current = self.scan_after(
            db,
            wire_root_id=self.wire_root_id,
            recipient_lookup=self.recipient_lookup,
        )
        if self.through_seq > current:
            self.upsert(db)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                columns=("recipient_lookup",),
                target=Participants,
                target_columns=("participant_lookup",),
            ),
        )


@dataclass(frozen=True, kw_only=True)
class AwarenessClaimGenerations(
    OwnerRecord, GuardedInserts, ImmutableRows, AwarenessTable, TypedTable
):

    insert_condition = """(NOT EXISTS (SELECT 1 FROM claim_batch_members m
        JOIN wake_claims c ON c.assignment_id=m.claim_id JOIN claim_batch_receipts r
        ON r.wire_root_id=m.wire_root_id AND r.wire_seq=m.wire_seq JOIN owner_generations g
        ON g.owner_lookup=m.recipient_lookup WHERE m.claim_id=NEW.claim_id
        AND m.wire_root_id=NEW.wire_root_id AND m.wire_seq=NEW.wire_seq
        AND m.recipient_lookup=NEW.recipient_lookup AND c.recipient_lookup=NEW.recipient_lookup
        AND c.message_id=r.message_id AND r.sealed=0
        AND g.generation=NEW.owner_generation))"""

    claim_id: Annotated[AssignmentId, Column(primary_key=True)]
    wire_root_id: RootId
    wire_seq: Annotated[int, Column(check="wire_seq > 0")]
    owner_lookup: Annotated[ThreadLookup, WireName("recipient_lookup"), Column(record_key=True)]
    owner_generation: Annotated[int, Column(check="owner_generation > 0")]

    def current(self, expected: OwnerGenerations) -> bool:
        """Old owner generations are historical; renaming preserves their identity."""
        if self.owner_lookup != expected.owner_lookup or self.owner_generation > expected.generation:
            raise ProjectionUnavailableError("captured participant owner changed")
        return self.owner_identity == expected

    def require_selection(self, assignment: WakeAssignment, acknowledgement: ClaimBatchAcknowledgements) -> None:
        """Verify this immutable acceptance capture against its original claim."""
        expected = type(self)(
            claim_id=assignment.assignment_id, wire_root_id=acknowledgement.wire_root_id,
            wire_seq=assignment.wire_seq, owner_lookup=assignment.recipient_lookup,
            owner_generation=self.owner_generation,
        )
        if self != expected:
            raise ProjectionUnavailableError("selected source generation provenance changed")

    def require_obligation(self, execution, link, assignment, obligation) -> None:
        """Captured ownership and the reply's original relational key agree."""
        execution.origin.require_obligation()
        original_owner = OwnerGenerations(
            owner_lookup=execution.owner_lookup,
            generation=self.owner_generation,
        )
        original_link = type(link)(
            execution_id=obligation.execution_id, assignment_id=self.claim_id,
            ordinal=link.ordinal,
        )
        if self.owner_identity != original_owner or link != original_link:
            raise ProjectionUnavailableError("open obligation has no exact owner provenance")
        if (self.claim_id, assignment.lifecycle.exact_target) != (
            assignment.assignment_id, obligation.exact_target
        ):
            raise ProjectionUnavailableError("open obligation has no exact owner provenance")

    @classmethod
    def references(cls):
        return (
            ForeignKey(columns=("claim_id",), target=WakeAssignment, target_columns=("assignment_id",)),
            ForeignKey(columns=("owner_lookup",), target=Participants, target_columns=("participant_lookup",)),
            ForeignKey(
                columns=("wire_root_id", "wire_seq", "claim_id"),
                target=ClaimBatchMembers,
                target_columns=("wire_root_id", "wire_seq", "claim_id"),
            ),
        )

def assert_optional_awareness_schema(db: sqlite3.Connection) -> None:
    if not AwarenessTable.stored_schema(db) or AwarenessTable.schema_differs(db):
        raise SchemaVersionError("optional awareness generation schema is unavailable")
