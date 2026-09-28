"""Declared cohort receipts and optional acceptance-generation provenance."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from .coordination import COORDINATION_SCHEMA_VERSION, Participants, SchemaVersionError, WakeClaims
from .coordination_store import MutationStore
from .typed_table import (
    Column,
    ForeignKey,
    Index,
    SQLiteForeignKeys,
    SQLiteJournalMode,
    SQLiteSchemaObject,
    TypedTable,
)

COHORT_SCHEMA_VERSION = 2
_LOG = logging.getLogger(__name__)


class CohortTable:
    """Mandatory sealed N/K receipt declarations."""


class AwarenessTable:
    """Optional provenance captured only during the unsealed acceptance transaction."""


@dataclass(frozen=True, kw_only=True)
class CohortSchemaMeta(CohortTable, TypedTable):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[2]
    ddl_digest: str = field(metadata={"sql": Column(check="length(ddl_digest) = 64")})

    @classmethod
    def triggers(cls):
        return {
            "cohort_meta_update_guard": (
                "CREATE TRIGGER cohort_meta_update_guard BEFORE UPDATE ON cohort_"
                "schema_meta\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort schema metadata is imm"
                "utable'); END"
            ),
            "cohort_meta_delete_guard": (
                "CREATE TRIGGER cohort_meta_delete_guard BEFORE DELETE ON cohort_"
                "schema_meta\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort schema metadata cannot"
                " be deleted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class ClaimBatchReceipts(CohortTable, TypedTable):
    wire_root_id: str = field(
        metadata={
            "sql": Column(
                primary_key=True,
                check=(
                    "\n"
                    "                length(wire_root_id) = 32 AND wire_root_id NOT G"
                    "LOB '*[^0-9a-f]*'"
                ),
            )
        }
    )
    wire_seq: int = field(metadata={"sql": Column(primary_key=True, check="wire_seq > 0")})
    message_id: str = field(metadata={"sql": Column(check="length(message_id) BETWEEN 1 AND 256")})
    exact_target: str = field(
        metadata={"sql": Column(check="length(exact_target) BETWEEN 1 AND 256")}
    )
    envelope_digest: str = field(metadata={"sql": Column(check="length(envelope_digest) = 64")})
    audience_digest: str = field(metadata={"sql": Column(check="length(audience_digest) = 64")})
    decisions_digest: str = field(metadata={"sql": Column(check="length(decisions_digest) = 64")})
    member_count: int = field(metadata={"sql": Column(check="member_count BETWEEN 0 AND 4096")})
    claim_count: int = field(
        metadata={"sql": Column(check="claim_count BETWEEN 0 AND member_count")}
    )
    manifest_codec: str = field(
        metadata={"sql": Column(check="length(manifest_codec) BETWEEN 1 AND 256")}
    )
    resolver_version: str = field(
        metadata={"sql": Column(check="length(resolver_version) BETWEEN 1 AND 256")}
    )
    policy_version: str = field(
        metadata={"sql": Column(check="length(policy_version) BETWEEN 1 AND 256")}
    )
    accepted_at_ms: int = field(metadata={"sql": Column(check="accepted_at_ms >= 0")})
    sealed: bool = field(default=False)
    without_rowid = True

    @classmethod
    def triggers(cls):
        return {
            "cohort_receipt_insert_guard": (
                "CREATE TRIGGER cohort_receipt_insert_guard\n"
                "        BEFORE INSERT ON claim_batch_receipts\n"
                "        WHEN NEW.sealed != 0\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort must be sealed after i"
                "ts member rows'); END"
            ),
            "cohort_receipt_update_guard": (
                "CREATE TRIGGER cohort_receipt_update_guard\n"
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
                "CREATE TRIGGER cohort_receipt_seal_complete\n"
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
                "CREATE TRIGGER cohort_receipt_delete_guard\n"
                "        BEFORE DELETE ON claim_batch_receipts\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort receipt cannot be dele"
                "ted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class ClaimBatchMembers(CohortTable, TypedTable):
    wire_root_id: str = field(metadata={"sql": Column(primary_key=True)})
    wire_seq: int = field(metadata={"sql": Column(primary_key=True)})
    ordinal: int = field(metadata={"sql": Column(primary_key=True, check="ordinal >= 0")})
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
            ForeignKey(("claim_id",), WakeClaims, ("claim_id",)),
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("wire_root_id", "wire_seq"), ClaimBatchReceipts, ("wire_root_id", "wire_seq")
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "cohort_members_insert_guard": (
                "CREATE TRIGGER cohort_members_insert_guard BEFORE INSERT ON clai"
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
                "                WHERE c.claim_id = NEW.claim_id\n"
                "                  AND c.recipient_lookup = NEW.recipient_lookup\n"
                "                  AND c.wire_seq = NEW.wire_seq\n"
                "                  AND c.message_id = r.message_id\n"
                "                  AND c.resolver_version = r.resolver_version\n"
                "                  AND c.policy_version = r.policy_version\n"
                "                  AND c.accepted_at_ms = r.accepted_at_ms);\n"
                "        END"
            ),
            "cohort_members_update_guard": (
                "CREATE TRIGGER cohort_members_update_guard BEFORE UPDATE ON clai"
                "m_batch_members\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort claim member is immuta"
                "ble'); END"
            ),
            "cohort_members_delete_guard": (
                "CREATE TRIGGER cohort_members_delete_guard BEFORE DELETE ON clai"
                "m_batch_members\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort claim member cannot be"
                " deleted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class CohortDeliveryReceipts(CohortTable, TypedTable):
    wire_root_id: str = field(metadata={"sql": Column(primary_key=True)})
    wire_seq: int = field(metadata={"sql": Column(primary_key=True)})
    ordinal: int = field(metadata={"sql": Column(primary_key=True, check="ordinal >= 0")})
    recipient_lookup: str
    canonical_thread: str = field(
        metadata={"sql": Column(check="length(canonical_thread) BETWEEN 1 AND 256")}
    )
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
    def references(cls):
        return (
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(("claim_id",), WakeClaims, ("claim_id",)),
            ForeignKey(
                ("wire_root_id", "wire_seq"), ClaimBatchReceipts, ("wire_root_id", "wire_seq")
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "cohort_delivery_insert_guard": (
                "CREATE TRIGGER cohort_delivery_insert_guard BEFORE INSERT ON coh"
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
                "                JOIN wake_claims c ON c.claim_id = m.claim_id\n"
                "                WHERE m.wire_root_id = NEW.wire_root_id AND m.wi"
                "re_seq = NEW.wire_seq\n"
                "                  AND m.claim_id = NEW.claim_id\n"
                "                  AND m.recipient_lookup = NEW.recipient_lookup\n"
                "                  AND c.recipient = NEW.canonical_thread);\n"
                "        END"
            ),
            "cohort_observer_claim_insert_guard": (
                "CREATE TRIGGER cohort_observer_claim_insert_guard BEFORE INSERT "
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
                "a legacy wake claim'); END"
            ),
            "cohort_delivery_update_guard": (
                "CREATE TRIGGER cohort_delivery_update_guard BEFORE UPDATE ON coh"
                "ort_delivery_receipts\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort delivery receipt is im"
                "mutable'); END"
            ),
            "cohort_delivery_delete_guard": (
                "CREATE TRIGGER cohort_delivery_delete_guard BEFORE DELETE ON coh"
                "ort_delivery_receipts\n"
                "        BEGIN SELECT RAISE(ABORT, 'cohort delivery receipt canno"
                "t be deleted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class AwarenessSchemaMeta(AwarenessTable, TypedTable):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[2]
    ddl_digest: str = field(metadata={"sql": Column(check="length(ddl_digest) = 64")})

    @classmethod
    def triggers(cls):
        return {
            "awareness_meta_update_guard": (
                "CREATE TRIGGER awareness_meta_update_guard BEFORE UPDATE ON awar"
                "eness_schema_meta\n"
                "        BEGIN SELECT RAISE(ABORT, 'optional schema metadata is i"
                "mmutable'); END"
            ),
            "awareness_meta_delete_guard": (
                "CREATE TRIGGER awareness_meta_delete_guard BEFORE DELETE ON awar"
                "eness_schema_meta\n"
                "        BEGIN SELECT RAISE(ABORT, 'optional schema metadata cann"
                "ot be deleted'); END"
            ),
        }


@dataclass(frozen=True, kw_only=True)
class AwarenessClaimGenerations(AwarenessTable, TypedTable):
    claim_id: str = field(metadata={"sql": Column(primary_key=True)})
    wire_root_id: str
    wire_seq: int = field(metadata={"sql": Column(check="wire_seq > 0")})
    recipient_lookup: str
    canonical_thread: str = field(
        metadata={"sql": Column(check="length(canonical_thread) BETWEEN 1 AND 256")}
    )
    owner_generation: int = field(metadata={"sql": Column(check="owner_generation > 0")})

    @classmethod
    def references(cls):
        return (
            ForeignKey(("claim_id",), WakeClaims, ("claim_id",)),
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("wire_root_id", "wire_seq", "claim_id"),
                ClaimBatchMembers,
                ("wire_root_id", "wire_seq", "claim_id"),
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "awareness_generation_insert_guard": (
                "CREATE TRIGGER awareness_generation_insert_guard\n"
                "        BEFORE INSERT ON awareness_claim_generations BEGIN\n"
                "            SELECT RAISE(ABORT, 'optional generation requires un"
                "sealed exact claim/member')\n"
                "            WHERE NOT EXISTS (\n"
                "                SELECT 1 FROM claim_batch_members m JOIN wake_cl"
                "aims c\n"
                "                  ON c.claim_id=m.claim_id JOIN claim_batch_rece"
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
                "CREATE TRIGGER awareness_generation_update_guard\n"
                "        BEFORE UPDATE ON awareness_claim_generations\n"
                "        BEGIN SELECT RAISE(ABORT, 'optional generation is immuta"
                "ble'); END"
            ),
            "awareness_generation_delete_guard": (
                "CREATE TRIGGER awareness_generation_delete_guard\n"
                "        BEFORE DELETE ON awareness_claim_generations\n"
                "        BEGIN SELECT RAISE(ABORT, 'optional generation cannot be"
                " deleted'); END"
            ),
        }


def _schema(capability: type) -> dict[str, str]:
    return {
        name: sql
        for table in TypedTable.members_with(capability)
        for name, sql in table.schema_objects().items()
    }


def _digest(schema: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(schema.values()).encode()).hexdigest()


def _cohort_objects(db: sqlite3.Connection) -> dict[str, str]:
    return {
        row.name: row.sql
        for row in SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
                "AND (name LIKE 'cohort_%' OR name LIKE 'claim_batch_%')"
            )
        )
    }


def _awareness_objects(db: sqlite3.Connection) -> dict[str, str]:
    return {
        row.name: row.sql
        for row in SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
                "AND name LIKE 'awareness_%'"
            )
        )
    }


def assert_cohort_schema(db: sqlite3.Connection) -> None:
    schema = _schema(CohortTable)
    try:
        meta = CohortSchemaMeta.one(db, singleton=1)
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise SchemaVersionError("private cohort schema is not installed") from error
    if meta != CohortSchemaMeta(
        singleton=1, version=COHORT_SCHEMA_VERSION, ddl_digest=_digest(schema)
    ):
        raise SchemaVersionError("unsupported private cohort schema version")
    if _cohort_objects(db) != schema or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise SchemaVersionError("private cohort schema is missing or drifted")


def assert_optional_awareness_schema(db: sqlite3.Connection) -> None:
    schema = _schema(AwarenessTable)
    meta = AwarenessSchemaMeta.one(db, singleton=1)
    if _awareness_objects(db) != schema or meta != AwarenessSchemaMeta(
        singleton=1, version=2, ddl_digest=_digest(schema)
    ):
        raise SchemaVersionError("optional awareness generation schema is unavailable")


def _install_optional_awareness_schema(db: sqlite3.Connection) -> None:
    if not _awareness_objects(db):
        for table in TypedTable.members_with(AwarenessTable):
            table.create(db)
        AwarenessSchemaMeta(
            singleton=1, version=2, ddl_digest=_digest(_schema(AwarenessTable))
        ).insert(db)
    assert_optional_awareness_schema(db)


def install_private_cohort_schema(store: MutationStore) -> None:
    """Create current receipt tables on a fresh coordinator; no old-schema repair."""
    if type(store) is not MutationStore:
        raise TypeError("cohort installation requires an initialized MutationStore")
    with store._transaction() as db:
        if store.schema_version != COORDINATION_SCHEMA_VERSION:
            raise SchemaVersionError("unsupported coordinator schema")
        if not _cohort_objects(db):
            for table in TypedTable.members_with(CohortTable):
                table.create(db)
            CohortSchemaMeta(
                singleton=1, version=COHORT_SCHEMA_VERSION, ddl_digest=_digest(_schema(CohortTable))
            ).insert(db)
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
        except (sqlite3.Error, SchemaVersionError, ValueError, TypeError) as error:
            db.execute("ROLLBACK TO optional_awareness_install")
            _LOG.warning("Optional awareness schema unavailable (%s)", type(error).__name__)
        finally:
            db.execute("RELEASE optional_awareness_install")
