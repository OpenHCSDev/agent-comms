"""Private coordinator cohort schema extension.

This module installs no authority during ordinary store construction. A sealed
receipt is a SQL cohort boundary, NOT proof of a bus manifest, owner identity,
or model-context delivery. No batch writer or runtime reader exists here.
"""

from __future__ import annotations

import hashlib
from typing import Final

from .coordination import COORDINATION_SCHEMA_VERSION, SchemaVersionError
from .coordination_store import MutationStore

COHORT_SCHEMA_VERSION: Final = 1

_DDL: Final[tuple[tuple[str, str], ...]] = (
    (
        "cohort_schema_meta",
        """CREATE TABLE cohort_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL CHECK (version = 1),
            ddl_digest TEXT NOT NULL CHECK (length(ddl_digest) = 64)
        ) STRICT""",
    ),
    (
        "claim_batch_receipts",
        """CREATE TABLE claim_batch_receipts (
            wire_root_id TEXT NOT NULL CHECK (
                length(wire_root_id) = 32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'),
            wire_seq INTEGER NOT NULL CHECK (wire_seq > 0),
            message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 256),
            exact_target TEXT NOT NULL CHECK (length(exact_target) BETWEEN 1 AND 256),
            envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
            audience_digest TEXT NOT NULL CHECK (length(audience_digest) = 64),
            decisions_digest TEXT NOT NULL CHECK (length(decisions_digest) = 64),
            member_count INTEGER NOT NULL CHECK (member_count BETWEEN 0 AND 4096),
            claim_count INTEGER NOT NULL CHECK (claim_count BETWEEN 0 AND member_count),
            manifest_codec TEXT NOT NULL CHECK (length(manifest_codec) BETWEEN 1 AND 256),
            resolver_version TEXT NOT NULL CHECK (length(resolver_version) BETWEEN 1 AND 256),
            policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 256),
            accepted_at_ms INTEGER NOT NULL CHECK (accepted_at_ms >= 0),
            sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
            PRIMARY KEY (wire_root_id, wire_seq)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "claim_batch_members",
        """CREATE TABLE claim_batch_members (
            wire_root_id TEXT NOT NULL,
            wire_seq INTEGER NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            claim_id TEXT NOT NULL REFERENCES wake_claims(claim_id),
            recipient_lookup TEXT NOT NULL REFERENCES participants(participant_lookup),
            PRIMARY KEY (wire_root_id, wire_seq, ordinal),
            UNIQUE (wire_root_id, wire_seq, claim_id),
            UNIQUE (claim_id),
            UNIQUE (wire_root_id, wire_seq, recipient_lookup),
            FOREIGN KEY (wire_root_id, wire_seq)
                REFERENCES claim_batch_receipts(wire_root_id, wire_seq)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "cohort_delivery_receipts",
        """CREATE TABLE cohort_delivery_receipts (
            wire_root_id TEXT NOT NULL,
            wire_seq INTEGER NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            recipient_lookup TEXT NOT NULL REFERENCES participants(participant_lookup),
            canonical_thread TEXT NOT NULL CHECK (length(canonical_thread) BETWEEN 1 AND 256),
            kind TEXT NOT NULL CHECK (kind IN ('selected', 'unmentioned_observer')),
            claim_id TEXT REFERENCES wake_claims(claim_id),
            PRIMARY KEY (wire_root_id, wire_seq, ordinal),
            UNIQUE (wire_root_id, wire_seq, recipient_lookup),
            UNIQUE (wire_root_id, wire_seq, canonical_thread),
            UNIQUE (wire_root_id, wire_seq, claim_id),
            FOREIGN KEY (wire_root_id, wire_seq)
                REFERENCES claim_batch_receipts(wire_root_id, wire_seq),
            CHECK ((kind = 'selected' AND claim_id IS NOT NULL)
                OR (kind = 'unmentioned_observer' AND claim_id IS NULL))
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "cohort_receipt_insert_guard",
        """CREATE TRIGGER cohort_receipt_insert_guard
        BEFORE INSERT ON claim_batch_receipts
        WHEN NEW.sealed != 0
        BEGIN SELECT RAISE(ABORT, 'cohort must be sealed after its member rows'); END""",
    ),
    (
        "cohort_receipt_update_guard",
        """CREATE TRIGGER cohort_receipt_update_guard
        BEFORE UPDATE ON claim_batch_receipts
        WHEN OLD.sealed != 0 OR NEW.sealed != 1
          OR NEW.wire_root_id != OLD.wire_root_id OR NEW.wire_seq != OLD.wire_seq
          OR NEW.message_id != OLD.message_id OR NEW.exact_target != OLD.exact_target
          OR NEW.envelope_digest != OLD.envelope_digest
          OR NEW.audience_digest != OLD.audience_digest
          OR NEW.decisions_digest != OLD.decisions_digest
          OR NEW.member_count != OLD.member_count OR NEW.claim_count != OLD.claim_count
          OR NEW.manifest_codec != OLD.manifest_codec
          OR NEW.resolver_version != OLD.resolver_version
          OR NEW.policy_version != OLD.policy_version
          OR NEW.accepted_at_ms != OLD.accepted_at_ms
        BEGIN SELECT RAISE(ABORT, 'cohort receipt facts are immutable'); END""",
    ),
    (
        "cohort_receipt_seal_complete",
        """CREATE TRIGGER cohort_receipt_seal_complete
        BEFORE UPDATE OF sealed ON claim_batch_receipts
        WHEN OLD.sealed = 0 AND NEW.sealed = 1
        BEGIN
            SELECT RAISE(ABORT, 'incomplete cohort delivery receipts')
            WHERE (SELECT COUNT(*) FROM cohort_delivery_receipts d
                   WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq)
                  != OLD.member_count
               OR (SELECT COUNT(*) FROM cohort_delivery_receipts d
                   WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq
                     AND d.kind = 'selected') != OLD.claim_count;
            SELECT RAISE(ABORT, 'incomplete cohort claim members')
            WHERE (SELECT COUNT(*) FROM claim_batch_members m
                   WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq)
                  != OLD.claim_count;
            SELECT RAISE(ABORT, 'noncontiguous cohort receipt ordinal')
            WHERE EXISTS (
                SELECT 1 FROM cohort_delivery_receipts d
                WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq
                  AND d.ordinal >= OLD.member_count)
               OR EXISTS (
                SELECT 1 FROM claim_batch_members m
                WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq
                  AND m.ordinal >= OLD.claim_count);
            SELECT RAISE(ABORT, 'cohort selected claim order does not match delivery order')
            WHERE EXISTS (
                SELECT 1 FROM claim_batch_members m
                LEFT JOIN cohort_delivery_receipts d
                  ON d.wire_root_id = m.wire_root_id AND d.wire_seq = m.wire_seq
                 AND d.claim_id = m.claim_id
                WHERE m.wire_root_id = OLD.wire_root_id AND m.wire_seq = OLD.wire_seq
                  AND (d.claim_id IS NULL OR m.ordinal != (
                    SELECT COUNT(*) FROM cohort_delivery_receipts preceding
                    WHERE preceding.wire_root_id = OLD.wire_root_id
                      AND preceding.wire_seq = OLD.wire_seq
                      AND preceding.kind = 'selected' AND preceding.ordinal < d.ordinal
                  ))
            );
            SELECT RAISE(ABORT, 'unmentioned observer has an existing wake claim')
            WHERE EXISTS (
                SELECT 1 FROM cohort_delivery_receipts d
                JOIN wake_claims c ON c.recipient_lookup = d.recipient_lookup
                  AND c.wire_seq = d.wire_seq
                WHERE d.wire_root_id = OLD.wire_root_id AND d.wire_seq = OLD.wire_seq
                  AND d.kind = 'unmentioned_observer'
            );
        END""",
    ),
    (
        "cohort_receipt_delete_guard",
        """CREATE TRIGGER cohort_receipt_delete_guard
        BEFORE DELETE ON claim_batch_receipts
        BEGIN SELECT RAISE(ABORT, 'cohort receipt cannot be deleted'); END""",
    ),
    (
        "cohort_meta_update_guard",
        """CREATE TRIGGER cohort_meta_update_guard BEFORE UPDATE ON cohort_schema_meta
        BEGIN SELECT RAISE(ABORT, 'cohort schema metadata is immutable'); END""",
    ),
    (
        "cohort_meta_delete_guard",
        """CREATE TRIGGER cohort_meta_delete_guard BEFORE DELETE ON cohort_schema_meta
        BEGIN SELECT RAISE(ABORT, 'cohort schema metadata cannot be deleted'); END""",
    ),
    (
        "cohort_members_insert_guard",
        """CREATE TRIGGER cohort_members_insert_guard BEFORE INSERT ON claim_batch_members
        BEGIN
            SELECT RAISE(ABORT, 'sealed cohort rejects members')
            WHERE (SELECT sealed FROM claim_batch_receipts r
                   WHERE r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq) != 0;
            SELECT RAISE(ABORT, 'claim member does not match cohort acceptance')
            WHERE NOT EXISTS (
                SELECT 1 FROM wake_claims c
                JOIN claim_batch_receipts r
                  ON r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq
                WHERE c.claim_id = NEW.claim_id
                  AND c.recipient_lookup = NEW.recipient_lookup
                  AND c.wire_seq = NEW.wire_seq
                  AND c.message_id = r.message_id
                  AND c.resolver_version = r.resolver_version
                  AND c.policy_version = r.policy_version
                  AND c.accepted_at_ms = r.accepted_at_ms);
        END""",
    ),
    (
        "cohort_delivery_insert_guard",
        """CREATE TRIGGER cohort_delivery_insert_guard BEFORE INSERT ON cohort_delivery_receipts
        BEGIN
            SELECT RAISE(ABORT, 'sealed cohort rejects delivery receipts')
            WHERE (SELECT sealed FROM claim_batch_receipts r
                   WHERE r.wire_root_id = NEW.wire_root_id AND r.wire_seq = NEW.wire_seq) != 0;
            SELECT RAISE(ABORT, 'selected delivery has no matching claim member')
            WHERE NEW.kind = 'selected' AND NOT EXISTS (
                SELECT 1 FROM claim_batch_members m
                JOIN wake_claims c ON c.claim_id = m.claim_id
                WHERE m.wire_root_id = NEW.wire_root_id AND m.wire_seq = NEW.wire_seq
                  AND m.claim_id = NEW.claim_id
                  AND m.recipient_lookup = NEW.recipient_lookup
                  AND c.recipient = NEW.canonical_thread);
        END""",
    ),
    (
        "cohort_observer_claim_insert_guard",
        """CREATE TRIGGER cohort_observer_claim_insert_guard BEFORE INSERT ON wake_claims
        WHEN EXISTS (
            SELECT 1 FROM cohort_delivery_receipts d
            JOIN claim_batch_receipts r ON r.wire_root_id=d.wire_root_id
              AND r.wire_seq=d.wire_seq AND r.sealed=1
            WHERE d.recipient_lookup=NEW.recipient_lookup AND d.wire_seq=NEW.wire_seq
              AND d.kind='unmentioned_observer')
        BEGIN SELECT RAISE(ABORT, 'no-wake observer cannot gain a legacy wake claim'); END""",
    ),
    (
        "cohort_members_update_guard",
        """CREATE TRIGGER cohort_members_update_guard BEFORE UPDATE ON claim_batch_members
        BEGIN SELECT RAISE(ABORT, 'cohort claim member is immutable'); END""",
    ),
    (
        "cohort_members_delete_guard",
        """CREATE TRIGGER cohort_members_delete_guard BEFORE DELETE ON claim_batch_members
        BEGIN SELECT RAISE(ABORT, 'cohort claim member cannot be deleted'); END""",
    ),
    (
        "cohort_delivery_update_guard",
        """CREATE TRIGGER cohort_delivery_update_guard BEFORE UPDATE ON cohort_delivery_receipts
        BEGIN SELECT RAISE(ABORT, 'cohort delivery receipt is immutable'); END""",
    ),
    (
        "cohort_delivery_delete_guard",
        """CREATE TRIGGER cohort_delivery_delete_guard BEFORE DELETE ON cohort_delivery_receipts
        BEGIN SELECT RAISE(ABORT, 'cohort delivery receipt cannot be deleted'); END""",
    ),
    (
        "cohort_receipt_owner_seq_idx",
        """CREATE INDEX cohort_receipt_owner_seq_idx
        ON cohort_delivery_receipts(recipient_lookup, wire_seq)""",
    ),
)

_DDL_DIGEST: Final = hashlib.sha256("\n".join(sql for _, sql in _DDL).encode()).hexdigest()
_EXPECTED_NAMES: Final = frozenset(name for name, _sql in _DDL)


def install_private_cohort_schema(store: MutationStore) -> None:
    """Opt-in atomic migration; no acceptance or bus-proof API is provided.

    A future reviewed batch writer must insert claims, receipt, K links and N
    delivery receipts inside ONE transaction, then seal it before COMMIT.
    """
    if type(store) is not MutationStore:
        raise TypeError("cohort migration requires an initialized MutationStore")
    with store._transaction() as db:
        if store.schema_version != COORDINATION_SCHEMA_VERSION:
            raise SchemaVersionError("unsupported frozen coordinator schema")
        actual: dict[str, str] = {
            row["name"]: row["sql"]
            for row in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
                "AND (name LIKE 'cohort_%' OR name LIKE 'claim_batch_%')"
            )
        }
        if not actual:
            for _name, statement in _DDL:
                db.execute(statement)
            db.execute(
                "INSERT INTO cohort_schema_meta(singleton,version,ddl_digest) VALUES (1,?,?)",
                (COHORT_SCHEMA_VERSION, _DDL_DIGEST),
            )
            actual = {
                row["name"]: row["sql"]
                for row in db.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
                    "AND (name LIKE 'cohort_%' OR name LIKE 'claim_batch_%')"
                )
            }
        if set(actual) != _EXPECTED_NAMES or any(actual.get(name) != sql for name, sql in _DDL):
            raise SchemaVersionError("private cohort schema structure is missing or changed")
        version = db.execute(
            "SELECT version,ddl_digest FROM cohort_schema_meta WHERE singleton=1"
        ).fetchone()
        if version is None or tuple(version) != (COHORT_SCHEMA_VERSION, _DDL_DIGEST):
            raise SchemaVersionError("private cohort schema version/digest is unsupported")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SchemaVersionError("cohort foreign keys must be enabled")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise SchemaVersionError("cohort schema requires rollback-journal mode")
