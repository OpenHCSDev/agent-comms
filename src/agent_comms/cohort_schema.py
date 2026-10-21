"""Declared cohort evidences and optional acceptance-generation provenance."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Annotated, Literal

from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.participants import OwnerGenerations, OwnerRecord, Participants
from agent_comms.coordinator import Coordination
from agent_comms.private_runtime_schema import PrivateRuntimeSchema

from .field_codec import MalformedRecord, WireName
from .typed_table import (
    Column,
    ColumnGuard,
    DurableTables,
    ForeignKey,
    FrozenColumns,
    GuardedColumns,
    GuardedInserts,
    GuardedRelatedInserts,
    GuardedUpdates,
    ImmutableRows,
    Index,
    NoDelete,
    SQLiteForeignKeys,
    SQLiteJournalMode,
    SharedFileTables,
    TypedTable,
)

_LOG = logging.getLogger(__name__)


class CohortTable(SharedFileTables, DurableTables, PrivateRuntimeSchema):
    """Mandatory sealed N/K acknowledgement declarations."""

    __slots__ = ()
    schema_error = SchemaVersionError

    @classmethod
    def install(cls, store) -> None:
        install_private_cohort_schema(store)


class AwarenessTable(SharedFileTables, DurableTables):
    """Optional provenance captured only during the unsealed acceptance transaction."""

    __slots__ = ()
    schema_error = SchemaVersionError


@dataclass(frozen=True, kw_only=True)
class ClaimBatchAcknowledgements(GuardedInserts, GuardedUpdates, FrozenColumns, GuardedColumns, NoDelete, CohortTable, TypedTable, declared_name="claim_batch_receipts"):

    insert_condition = """NEW.sealed != 0"""
    immutable_columns = ('wire_root_id', 'wire_seq', 'message_id', 'exact_target', 'envelope_digest', 'audience_digest', 'decisions_digest', 'member_count', 'claim_count', 'manifest_codec', 'resolver_version', 'policy_version', 'accepted_at_ms')
    update_condition = """OLD.sealed != 0 OR NEW.sealed != 1"""
    column_guards = (
        ColumnGuard(('sealed',), """(OLD.sealed = 0 AND NEW.sealed = 1) AND (((SELECT COUNT(*) FROM cohort_delivery_receipts d WHERE
        d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq) != OLD.member_count OR (SELECT COUNT(*)
        FROM cohort_delivery_receipts d WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq AND
        d.kind = 'selected') != OLD.claim_count) OR ((SELECT COUNT(*) FROM claim_batch_members m WHERE
        m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq) != OLD.claim_count) OR (EXISTS ( SELECT 1
        FROM cohort_delivery_receipts d WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq AND
        d.ordinal >= OLD.member_count) OR EXISTS ( SELECT 1 FROM claim_batch_members m WHERE m.wire_root_id =
        OLD.wire_root_id AND m.wire_seq = OLD.wire_seq AND m.ordinal >= OLD.claim_count)) OR (EXISTS ( SELECT 1 FROM
        claim_batch_members m LEFT JOIN cohort_delivery_receipts d ON d.wire_root_id = m.wire_root_id AND d.wire_seq
        = m.wire_seq AND d.claim_id = m.claim_id WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq =
        OLD.wire_seq AND (d.claim_id IS NULL OR m.ordinal != ( SELECT COUNT(*) FROM cohort_delivery_receipts
        preceding WHERE preceding.wire_root_id = OLD.wire_root_id AND preceding.wire_seq = OLD.wire_seq AND
        preceding.kind = 'selected' AND preceding.ordinal < d.ordinal )) )) OR (EXISTS ( SELECT 1 FROM
        cohort_delivery_receipts d JOIN wake_claims c ON c.recipient_lookup = d.recipient_lookup AND c.wire_seq =
        d.wire_seq WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq AND d.kind =
        'unmentioned_observer' )))"""),
    )

    wire_root_id: Annotated[
        str,
        Column(
            primary_key=True,
            check=(
                "\n"
                "                length(wire_root_id) = 32 AND wire_root_id NOT G"
                "LOB '*[^0-9a-f]*'"
            ),
        ),
    ]
    wire_seq: Annotated[int, Column(primary_key=True, check="wire_seq > 0")]
    message_id: Annotated[str, Column(check="length(message_id) BETWEEN 1 AND 256")]
    exact_target: Annotated[str, Column(check="length(exact_target) BETWEEN 1 AND 256")]
    envelope_digest: Annotated[str, Column(check="length(envelope_digest) = 64")]
    audience_digest: Annotated[str, Column(check="length(audience_digest) = 64")]
    decisions_digest: Annotated[str, Column(check="length(decisions_digest) = 64")]
    member_count: Annotated[int, Column(check="member_count BETWEEN 0 AND 4096")]
    claim_count: Annotated[int, Column(check="claim_count BETWEEN 0 AND member_count")]
    manifest_codec: Annotated[str, Column(check="length(manifest_codec) BETWEEN 1 AND 256")]
    resolver_version: Annotated[
        str, Column(check="length(resolver_version) BETWEEN 1 AND 256")
    ]
    policy_version: Annotated[str, Column(check="length(policy_version) BETWEEN 1 AND 256")]
    accepted_at_ms: Annotated[int, Column(check="accepted_at_ms >= 0")]
    sealed: bool = False
    without_rowid = True

    def require_selected_claim(self, assignment: WakeAssignment) -> None:
        """An awareness contribution borrows this original sealed acknowledgement."""
        from .wake_candidate_index import ProjectionUnavailableError

        if not self.sealed or self.message_id != assignment.message_id:
            raise ProjectionUnavailableError("selected source acknowledgement is unsealed or changed")

@dataclass(frozen=True, kw_only=True)
class ClaimBatchMembers(GuardedInserts, ImmutableRows, CohortTable, TypedTable):

    insert_condition = """((SELECT sealed FROM claim_batch_receipts r WHERE r.wire_root_id = NEW.wire_root_id AND r.wire_seq =
        NEW.wire_seq) != 0) OR (NOT EXISTS ( SELECT 1 FROM wake_claims c JOIN claim_batch_receipts r ON
        r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq WHERE c.assignment_id = NEW.claim_id AND
        c.recipient_lookup = NEW.recipient_lookup AND c.wire_seq = NEW.wire_seq AND c.message_id = r.message_id AND
        c.resolver_version = r.resolver_version AND c.policy_version = r.policy_version AND c.accepted_at_ms =
        r.accepted_at_ms))"""

    wire_root_id: Annotated[str, Column(primary_key=True)]
    wire_seq: Annotated[int, Column(primary_key=True)]
    ordinal: Annotated[int, Column(primary_key=True, check="ordinal >= 0")]
    claim_id: str
    recipient_lookup: str
    unique = (
        ("wire_root_id", "wire_seq", "claim_id"),
        ("claim_id",),
        ("wire_root_id", "wire_seq", "recipient_lookup"),
    )
    without_rowid = True

    @classmethod
    def references(cls):
        return (
            ForeignKey(("claim_id",), WakeAssignment, ("assignment_id",)),
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("wire_root_id", "wire_seq"), ClaimBatchAcknowledgements, ("wire_root_id", "wire_seq")
            ),
        )

@dataclass(frozen=True, kw_only=True)
class CohortDeliveryReceipts(GuardedRelatedInserts, GuardedInserts, ImmutableRows, CohortTable, TypedTable):
    # Delivery selection kind is distinct from this table's nominal identity.


    related_insert_target = WakeAssignment
    related_insert_condition = """EXISTS ( SELECT 1 FROM cohort_delivery_receipts d JOIN claim_batch_receipts r ON
        r.wire_root_id=d.wire_root_id AND r.wire_seq=d.wire_seq AND r.sealed=1 WHERE
        d.recipient_lookup=NEW.recipient_lookup AND d.wire_seq=NEW.wire_seq AND d.kind='unmentioned_observer')"""
    insert_condition = """((SELECT sealed FROM claim_batch_receipts r WHERE r.wire_root_id = NEW.wire_root_id AND r.wire_seq =
        NEW.wire_seq) != 0) OR (NEW.kind = 'selected' AND NOT EXISTS ( SELECT 1 FROM claim_batch_members m JOIN
        wake_claims c ON c.assignment_id = m.claim_id WHERE m.wire_root_id = NEW.wire_root_id AND m.wire_seq =
        NEW.wire_seq AND m.claim_id = NEW.claim_id AND m.recipient_lookup = NEW.recipient_lookup AND c.recipient =
        NEW.canonical_thread))"""

    wire_root_id: Annotated[str, Column(primary_key=True)]
    wire_seq: Annotated[int, Column(primary_key=True)]
    ordinal: Annotated[int, Column(primary_key=True, check="ordinal >= 0")]
    recipient_lookup: str
    canonical_thread: Annotated[
        str, Column(check="length(canonical_thread) BETWEEN 1 AND 256")
    ]
    kind: Literal["selected", "unmentioned_observer"]
    claim_id: str | None
    checks = (
        (
            "(kind = 'selected' AND claim_id IS NOT NULL)\n"
            "                OR (kind = 'unmentioned_observer' AND claim_id I"
            "S NULL)"
        ),
    )
    unique = (
        ("wire_root_id", "wire_seq", "recipient_lookup"),
        ("wire_root_id", "wire_seq", "canonical_thread"),
        ("wire_root_id", "wire_seq", "claim_id"),
    )
    indexes = (Index(("recipient_lookup", "wire_seq")),)
    without_rowid = True

    @classmethod
    def sealed_sequences_after(
        cls,
        db: sqlite3.Connection,
        *,
        wire_root_id: str,
        recipient_lookup: str,
        after_seq: int,
    ) -> frozenset[int]:
        """Sealed addressed evidences after one recipient's visited position."""
        rows = cls.read(
            db.execute(
                "SELECT d.* FROM cohort_delivery_receipts d "
                "JOIN claim_batch_receipts r ON r.wire_root_id=d.wire_root_id "
                "AND r.wire_seq=d.wire_seq AND r.sealed=1 "
                "WHERE d.wire_root_id=? AND d.recipient_lookup=? AND d.wire_seq>? "
                "ORDER BY d.wire_seq",
                (wire_root_id, recipient_lookup, after_seq),
            )
        )
        return frozenset(row.wire_seq for row in rows)

    @classmethod
    def references(cls):
        return (
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(("claim_id",), WakeAssignment, ("assignment_id",)),
            ForeignKey(
                ("wire_root_id", "wire_seq"), ClaimBatchAcknowledgements, ("wire_root_id", "wire_seq")
            ),
        )

@dataclass(frozen=True, kw_only=True)
class AwarenessClaimGenerations(OwnerRecord, GuardedInserts, ImmutableRows, AwarenessTable, TypedTable):

    insert_condition = """(NOT EXISTS ( SELECT 1 FROM claim_batch_members m JOIN wake_claims c ON c.assignment_id=m.claim_id JOIN
        claim_batch_receipts r ON r.wire_root_id=m.wire_root_id AND r.wire_seq=m.wire_seq JOIN owner_generations g
        ON g.owner_lookup=m.recipient_lookup WHERE m.claim_id=NEW.claim_id AND m.wire_root_id=NEW.wire_root_id AND
        m.wire_seq=NEW.wire_seq AND m.recipient_lookup=NEW.recipient_lookup AND c.recipient=NEW.canonical_thread AND
        c.message_id=r.message_id AND r.sealed=0 AND g.owner_thread=NEW.canonical_thread AND
        g.generation=NEW.owner_generation))"""

    claim_id: Annotated[str, Column(primary_key=True)]
    wire_root_id: str
    wire_seq: Annotated[int, Column(check="wire_seq > 0")]
    owner_lookup: Annotated[str, WireName("recipient_lookup"), Column(record_key=True)]
    owner_thread: Annotated[
        str, WireName("canonical_thread"), Column(record_key=True, check="length(canonical_thread) BETWEEN 1 AND 256")
    ]
    owner_generation: Annotated[int, Column(check="owner_generation > 0")]

    def current(self, expected: OwnerGenerations) -> bool:
        """A historical capture is omitted; a changed current owner refuses."""
        from .wake_candidate_index import ProjectionUnavailableError

        if self.owner_generation < expected.generation:
            return False
        if self.owner_identity != expected:
            raise ProjectionUnavailableError("captured participant owner changed")
        return True

    def require_selection(self, assignment: WakeAssignment, acknowledgement: ClaimBatchAcknowledgements) -> None:
        """Verify this immutable acceptance capture against its original claim."""
        from .wake_candidate_index import ProjectionUnavailableError

        expected = type(self)(
            claim_id=assignment.assignment_id, wire_root_id=acknowledgement.wire_root_id,
            wire_seq=assignment.wire_seq, owner_lookup=assignment.recipient_lookup,
            owner_thread=assignment.recipient, owner_generation=self.owner_generation,
        )
        if self != expected:
            raise ProjectionUnavailableError("selected source generation provenance changed")

    def require_obligation(self, execution, link, assignment, obligation) -> None:
        """Captured ownership and the reply's original relational key agree."""
        from .coordination_tables.executions import ExecutionOrigin
        from .wake_candidate_index import ProjectionUnavailableError

        if execution.origin is not ExecutionOrigin.WIRE:
            raise ProjectionUnavailableError("open obligation has no wire origin")
        original_owner = OwnerGenerations(
            owner_lookup=execution.owner_lookup, owner_thread=execution.owner_thread,
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
            ForeignKey(("claim_id",), WakeAssignment, ("assignment_id",)),
            ForeignKey(("owner_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("wire_root_id", "wire_seq", "claim_id"),
                ClaimBatchMembers,
                ("wire_root_id", "wire_seq", "claim_id"),
            ),
        )

def assert_optional_awareness_schema(db: sqlite3.Connection) -> None:
    if not AwarenessTable.stored_schema(db) or AwarenessTable.schema_differs(db):
        raise SchemaVersionError("optional awareness generation schema is unavailable")


def _install_optional_awareness_schema(db: sqlite3.Connection) -> None:
    if not AwarenessTable.stored_schema(db):
        AwarenessTable.create_schema(db)
    assert_optional_awareness_schema(db)


def install_private_cohort_schema(store: Coordination) -> None:
    """Create current acknowledgement tables on a fresh coordinator; no old-schema repair."""
    with store.session.writing() as db:
        CoordinatorTable.require_schema(db)
        if not CohortTable.stored_schema(db):
            CohortTable.create_schema(db)
        CohortTable.require_installed(db)
        if SQLiteJournalMode.read(db.execute("PRAGMA journal_mode")) != [
            SQLiteJournalMode("delete")
        ]:
            raise SchemaVersionError("cohort schema requires rollback-journal mode")
        # Missing/corrupt optional metadata must not roll back mandatory cohort
        # creation, and no prior claim is retroactively assigned a generation.
        db.execute("SAVEPOINT optional_awareness_install")
        try:
            _install_optional_awareness_schema(db)
        except (sqlite3.Error, SchemaVersionError, MalformedRecord) as error:
            db.execute("ROLLBACK TO optional_awareness_install")
            _LOG.warning("Optional awareness schema unavailable: %r", error, exc_info=error)
        finally:
            db.execute("RELEASE optional_awareness_install")
