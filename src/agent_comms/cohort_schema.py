"""Declared cohort evidences and optional acceptance-generation provenance."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from agent_comms.coordination_errors import IdentityConflict, SchemaVersionError
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.participants import OwnerGenerations, Participants
from agent_comms.coordinator import Coordination
from agent_comms.private_runtime_schema import PrivateRuntimeSchema

from .audience_manifest import MAX_AUDIENCE_BYTES
from .coordination_contracts import POLICY_VERSION, RESOLVER_VERSION
from .field_codec import MalformedRecord
from .response_acknowledgement import _canonical
from .typed_table import (
    Column,
    DurableTables,
    ForeignKey,
    Index,
    SharedFileTables,
    SQLiteForeignKeys,
    SQLiteJournalMode,
    TypedTable,
)

if TYPE_CHECKING:
    from .bus_publication import Delivered
    from .private_registry_guard import RegistryPrivacy

_LOG = logging.getLogger(__name__)


class CohortTable(PrivateRuntimeSchema):
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
class ClaimBatchAcknowledgements(CohortTable, TypedTable, declared_name="claim_batch_receipts"):
    manifest_format: ClassVar[str] = "audience-v1"
    decision_domain: ClassVar[bytes] = b"agent-comms:cohort-decisions:v1\0"

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
            envelope_digest=delivery.audience.wire_envelope_digest,
            audience_digest=delivery.audience.digest,
            decisions_digest=cls.decision_hash(delivery),
            member_count=len(delivery.audience.recipients),
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
        from .wake_candidate_index import ProjectionUnavailableError

        if not self.sealed or self.message_id != assignment.message_id:
            raise ProjectionUnavailableError("selected source acknowledgement is unsealed or changed")

    @classmethod
    def trigger_bodies(cls):
        return {
            "cohort_receipt_insert_guard": (
                "\n"
                "        BEFORE INSERT ON claim_batch_receipts\n"
                "        WHEN NEW.sealed != 0\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort must be sealed after i"
                "ts member rows'); END"
            ),
            "cohort_receipt_update_guard": (
                "\n"
                "        BEFORE UPDATE ON claim_batch_receipts\n"
                "        WHEN OLD.sealed != 0 OR NEW.sealed != 1\n"
                "          OR NEW.wire_root_id != OLD.wire_root_id OR NEW.wire_se"
                "q != OLD.wire_seq\n"
                "          OR NEW.message_id != OLD.message_id OR NEW.exact_targe"
                "t != OLD.exact_target\n"
                "          OR NEW.envelope_digest != OLD.envelope_digest\n"
                "          OR NEW.audience_digest != OLD.audience_digest\n"
                "          OR NEW.decisions_digest != OLD.decisions_digest\n"
                "          OR NEW.member_count != OLD.member_count OR NEW.claim_c"
                "ount != OLD.claim_count\n"
                "          OR NEW.manifest_codec != OLD.manifest_codec\n"
                "          OR NEW.resolver_version != OLD.resolver_version\n"
                "          OR NEW.policy_version != OLD.policy_version\n"
                "          OR NEW.accepted_at_ms != OLD.accepted_at_ms\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort receipt facts are immu"
                "table'); END"
            ),
            "cohort_receipt_seal_complete": (
                "\n"
                "        BEFORE UPDATE OF sealed ON claim_batch_receipts\n"
                "        WHEN OLD.sealed = 0 AND NEW.sealed = 1\n"
                "        BEGIN\n"
                "            SELECT RAISE(ABORT, 'incomplete cohort delivery rece"
                "ipts')\n"
                "            WHERE (SELECT COUNT(*) FROM cohort_delivery_receipts"
                " d\n"
                "                   WHERE d.wire_root_id = OLD.wire_root_id AND d"
                ".wire_seq = OLD.wire_seq)\n"
                "                  != OLD.member_count\n"
                "               OR (SELECT COUNT(*) FROM cohort_delivery_receipts"
                " d\n"
                "                   WHERE d.wire_root_id = OLD.wire_root_id AND d"
                ".wire_seq = OLD.wire_seq\n"
                "                     AND d.kind = 'selected') != OLD.claim_count"
                ";\n"
                "            SELECT RAISE(ABORT, 'incomplete cohort claim members"
                "')\n"
                "            WHERE (SELECT COUNT(*) FROM claim_batch_members m\n"
                "                   WHERE m.wire_root_id = OLD.wire_root_id AND m"
                ".wire_seq = OLD.wire_seq)\n"
                "                  != OLD.claim_count;\n"
                "            SELECT RAISE(ABORT, 'noncontiguous cohort receipt or"
                "dinal')\n"
                "            WHERE EXISTS (\n"
                "                SELECT 1 FROM cohort_delivery_receipts d\n"
                "                WHERE d.wire_root_id = OLD.wire_root_id AND d.wi"
                "re_seq = OLD.wire_seq\n"
                "                  AND d.ordinal >= OLD.member_count)\n"
                "               OR EXISTS (\n"
                "                SELECT 1 FROM claim_batch_members m\n"
                "                WHERE m.wire_root_id = OLD.wire_root_id AND m.wi"
                "re_seq = OLD.wire_seq\n"
                "                  AND m.ordinal >= OLD.claim_count);\n"
                "            SELECT RAISE(ABORT, 'cohort selected claim order doe"
                "s not match delivery order')\n"
                "            WHERE EXISTS (\n"
                "                SELECT 1 FROM claim_batch_members m\n"
                "                LEFT JOIN cohort_delivery_receipts d\n"
                "                  ON d.wire_root_id = m.wire_root_id AND d.wire_"
                "seq = m.wire_seq\n"
                "                 AND d.claim_id = m.claim_id\n"
                "                WHERE m.wire_root_id = OLD.wire_root_id AND m.wi"
                "re_seq = OLD.wire_seq\n"
                "                  AND (d.claim_id IS NULL OR m.ordinal != (\n"
                "                    SELECT COUNT(*) FROM cohort_delivery_receipt"
                "s preceding\n"
                "                    WHERE preceding.wire_root_id = OLD.wire_root"
                "_id\n"
                "                      AND preceding.wire_seq = OLD.wire_seq\n"
                "                      AND preceding.kind = 'selected' AND preced"
                "ing.ordinal < d.ordinal\n"
                "                  ))\n"
                "            );\n"
                "            SELECT RAISE(ABORT, 'unmentioned observer has an exi"
                "sting wake claim')\n"
                "            WHERE EXISTS (\n"
                "                SELECT 1 FROM cohort_delivery_receipts d\n"
                "                JOIN wake_claims c ON c.recipient_lookup = d.rec"
                "ipient_lookup\n"
                "                  AND c.wire_seq = d.wire_seq\n"
                "                WHERE d.wire_root_id = OLD.wire_root_id AND d.wi"
                "re_seq = OLD.wire_seq\n"
                "                  AND d.kind = 'unmentioned_observer'\n"
                "            );\n"
                "        END"
            ),
            "cohort_receipt_delete_guard": (
                "\n"
                "        BEFORE DELETE ON claim_batch_receipts\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort receipt cannot be dele"
                "ted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class ClaimBatchMembers(CohortTable, TypedTable):
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

    @classmethod
    def trigger_bodies(cls):
        return {
            "cohort_members_insert_guard": (
                " BEFORE INSERT ON clai"
                "m_batch_members\n"
                "        BEGIN\n"
                "            SELECT RAISE(ABORT, 'sealed cohort rejects members')"
                "\n"
                "            WHERE (SELECT sealed FROM claim_batch_receipts r\n"
                "                   WHERE r.wire_root_id = NEW.wire_root_id AND r"
                ".wire_seq = NEW.wire_seq) != 0;\n"
                "            SELECT RAISE(ABORT, 'claim member does not match coh"
                "ort acceptance')\n"
                "            WHERE NOT EXISTS (\n"
                "                SELECT 1 FROM wake_claims c\n"
                "                JOIN claim_batch_receipts r\n"
                "                  ON r.wire_root_id = NEW.wire_root_id AND r.wir"
                "e_seq = NEW.wire_seq\n"
                "                WHERE c.assignment_id = NEW.claim_id\n"
                "                  AND c.recipient_lookup = NEW.recipient_lookup\n"
                "                  AND c.wire_seq = NEW.wire_seq\n"
                "                  AND c.message_id = r.message_id\n"
                "                  AND c.resolver_version = r.resolver_version\n"
                "                  AND c.policy_version = r.policy_version\n"
                "                  AND c.accepted_at_ms = r.accepted_at_ms);\n"
                "        END"
            ),
            "cohort_members_update_guard": (
                " BEFORE UPDATE ON clai"
                "m_batch_members\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort claim member is immuta"
                "ble'); END"
            ),
            "cohort_members_delete_guard": (
                " BEFORE DELETE ON clai"
                "m_batch_members\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort claim member cannot be"
                " deleted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class CohortDeliveryReceipts(CohortTable, TypedTable):
    # Delivery selection kind is distinct from this table's nominal identity.

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

    @classmethod
    def trigger_bodies(cls):
        return {
            "cohort_delivery_insert_guard": (
                " BEFORE INSERT ON coh"
                "ort_delivery_receipts\n"
                "        BEGIN\n"
                "            SELECT RAISE(ABORT, 'sealed cohort rejects delivery "
                "receipts')\n"
                "            WHERE (SELECT sealed FROM claim_batch_receipts r\n"
                "                   WHERE r.wire_root_id = NEW.wire_root_id AND r"
                ".wire_seq = NEW.wire_seq) != 0;\n"
                "            SELECT RAISE(ABORT, 'selected delivery has no matchi"
                "ng claim member')\n"
                "            WHERE NEW.kind = 'selected' AND NOT EXISTS (\n"
                "                SELECT 1 FROM claim_batch_members m\n"
                "                JOIN wake_claims c ON c.assignment_id = m.claim_id\n"
                "                WHERE m.wire_root_id = NEW.wire_root_id AND m.wi"
                "re_seq = NEW.wire_seq\n"
                "                  AND m.claim_id = NEW.claim_id\n"
                "                  AND m.recipient_lookup = NEW.recipient_lookup\n"
                "                  AND c.recipient = NEW.canonical_thread);\n"
                "        END"
            ),
            "cohort_observer_claim_insert_guard": (
                " BEFORE INSERT "
                "ON wake_claims\n"
                "        WHEN EXISTS (\n"
                "            SELECT 1 FROM cohort_delivery_receipts d\n"
                "            JOIN claim_batch_receipts r ON r.wire_root_id=d.wire"
                "_root_id\n"
                "              AND r.wire_seq=d.wire_seq AND r.sealed=1\n"
                "            WHERE d.recipient_lookup=NEW.recipient_lookup AND d."
                "wire_seq=NEW.wire_seq\n"
                "              AND d.kind='unmentioned_observer')\n"
                "        BEGIN SELECT RAISE(ABORT, 'no-wake observer cannot gain "
                "an unbound wake claim'); END"
            ),
            "cohort_delivery_update_guard": (
                " BEFORE UPDATE ON coh"
                "ort_delivery_receipts\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort delivery receipt is im"
                "mutable'); END"
            ),
            "cohort_delivery_delete_guard": (
                " BEFORE DELETE ON coh"
                "ort_delivery_receipts\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort delivery receipt canno"
                "t be deleted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class AwarenessClaimGenerations(AwarenessTable, TypedTable):
    claim_id: Annotated[str, Column(primary_key=True)]
    wire_root_id: str
    wire_seq: Annotated[int, Column(check="wire_seq > 0")]
    recipient_lookup: str
    canonical_thread: Annotated[
        str, Column(check="length(canonical_thread) BETWEEN 1 AND 256")
    ]
    owner_generation: Annotated[int, Column(check="owner_generation > 0")]

    def current(self, expected: OwnerGenerations) -> bool:
        """Prior generations/names are historical; wrong or future identities refuse."""
        from .wake_candidate_index import ProjectionUnavailableError

        if self.recipient_lookup != expected.owner_lookup or self.owner_generation > expected.generation:
            raise ProjectionUnavailableError("captured participant owner changed")
        return self.captured_owner == expected

    @property
    def captured_owner(self) -> OwnerGenerations:
        """The original acceptance's owner observation, not a current lease."""
        return OwnerGenerations(
            owner_lookup=self.recipient_lookup, owner_thread=self.canonical_thread,
            generation=self.owner_generation,
        )

    def require_selection(self, assignment: WakeAssignment, acknowledgement: ClaimBatchAcknowledgements) -> None:
        """Verify this immutable acceptance capture against its original claim."""
        from .wake_candidate_index import ProjectionUnavailableError

        expected = type(self)(
            claim_id=assignment.assignment_id, wire_root_id=acknowledgement.wire_root_id,
            wire_seq=assignment.wire_seq, recipient_lookup=assignment.recipient_lookup,
            canonical_thread=assignment.recipient, owner_generation=self.owner_generation,
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
        if self.captured_owner != original_owner or link != original_link:
            raise ProjectionUnavailableError("open obligation has no exact owner provenance")
        if (self.claim_id, assignment.lifecycle.exact_target) != (
            assignment.assignment_id, obligation.exact_target
        ):
            raise ProjectionUnavailableError("open obligation has no exact owner provenance")

    @classmethod
    def references(cls):
        return (
            ForeignKey(("claim_id",), WakeAssignment, ("assignment_id",)),
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("wire_root_id", "wire_seq", "claim_id"),
                ClaimBatchMembers,
                ("wire_root_id", "wire_seq", "claim_id"),
            ),
        )

    @classmethod
    def trigger_bodies(cls):
        return {
            "awareness_generation_insert_guard": (
                "\n"
                "        BEFORE INSERT ON awareness_claim_generations BEGIN\n"
                "            SELECT RAISE(ABORT, 'optional generation requires un"
                "sealed exact claim/member')\n"
                "            WHERE NOT EXISTS (\n"
                "                SELECT 1 FROM claim_batch_members m JOIN wake_cl"
                "aims c\n"
                "                  ON c.assignment_id=m.claim_id JOIN claim_batch_rece"
                "ipts r\n"
                "                  ON r.wire_root_id=m.wire_root_id AND r.wire_se"
                "q=m.wire_seq\n"
                "                JOIN owner_generations g ON g.owner_lookup=m.rec"
                "ipient_lookup\n"
                "                WHERE m.claim_id=NEW.claim_id AND m.wire_root_id"
                "=NEW.wire_root_id\n"
                "                  AND m.wire_seq=NEW.wire_seq\n"
                "                  AND m.recipient_lookup=NEW.recipient_lookup\n"
                "                  AND c.recipient=NEW.canonical_thread\n"
                "                  AND c.message_id=r.message_id AND r.sealed=0\n"
                "                  AND g.owner_thread=NEW.canonical_thread\n"
                "                  AND g.generation=NEW.owner_generation);\n"
                "        END"
            ),
            "awareness_generation_update_guard": (
                "\n"
                "        BEFORE UPDATE ON awareness_claim_generations\n"
                "        BEGIN SELECT RAISE(ABORT, 'optional generation is immuta"
                "ble'); END"
            ),
            "awareness_generation_delete_guard": (
                "\n"
                "        BEFORE DELETE ON awareness_claim_generations\n"
                "        BEGIN SELECT RAISE(ABORT, 'optional generation cannot be"
                " deleted'); END"
            ),
        }


def assert_cohort_schema(db: sqlite3.Connection) -> None:
    if not CohortTable.stored_schema(db):
        raise SchemaVersionError("private cohort schema is not installed")
    if CohortTable.schema_differs(db) or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise SchemaVersionError("private cohort schema is missing or drifted")


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
        assert_cohort_schema(db)
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
