"""Private same-DB input attempts for the coordinated runner.

A row reserves one input ID *before* Pi starts. Its optional context fields are
written only from a live native Pi event, never reconstructed from a journal on
restart. An incomplete row is uncertain and must not trigger automatic retry.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from .coordination_store import MutationStore, PublicationActivationBlocked

_DDL = (
    (
        "native_runtime_schema_meta",
        """CREATE TABLE native_runtime_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL CHECK (version = 2),
            ddl_digest TEXT NOT NULL CHECK (length(ddl_digest) = 64)
        ) STRICT""",
    ),
    (
        "native_runtime_inputs",
        """CREATE TABLE native_runtime_inputs (
            input_id TEXT PRIMARY KEY CHECK (
                length(input_id) = 32 AND input_id NOT GLOB '*[^0-9a-f]*'),
            stage TEXT NOT NULL CHECK (stage IN ('triage','full')),
            claim_id TEXT NOT NULL REFERENCES wake_claims(claim_id),
            execution_id TEXT REFERENCES executions(execution_id),
            attempt_ordinal INTEGER,
            owner_lookup TEXT NOT NULL REFERENCES participants(participant_lookup),
            owner_thread TEXT NOT NULL,
            owner_generation INTEGER NOT NULL CHECK (owner_generation > 0),
            owner_token_digest TEXT NOT NULL CHECK (length(owner_token_digest) = 64),
            session_id TEXT,
            session_file TEXT,
            session_entry_id TEXT,
            request_generation INTEGER,
            llm_context_digest TEXT,
            verdict TEXT CHECK (verdict IN ('ignore','full')),
            CHECK (
                (stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL)
                OR (stage='full' AND execution_id IS NOT NULL AND attempt_ordinal > 0)),
            CHECK (
                (session_id IS NULL AND session_file IS NULL
                    AND session_entry_id IS NULL AND request_generation IS NULL
                    AND llm_context_digest IS NULL)
                OR (session_id IS NOT NULL AND length(session_id)>0
                    AND session_file IS NOT NULL AND length(session_file)>0
                    AND session_entry_id IS NOT NULL AND length(session_entry_id)>0
                    AND request_generation > 0 AND length(llm_context_digest)=64)),
            CHECK ((stage='triage') OR verdict IS NULL),
            UNIQUE(stage,claim_id),
            UNIQUE(execution_id,attempt_ordinal)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "native_runtime_source_cursors",
        """CREATE TABLE native_runtime_source_cursors (
            wire_root_id TEXT NOT NULL CHECK (
                length(wire_root_id)=32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'),
            recipient_lookup TEXT NOT NULL REFERENCES participants(participant_lookup),
            owner_thread TEXT NOT NULL CHECK (owner_thread <> ''),
            owner_generation INTEGER NOT NULL CHECK (owner_generation > 0),
            owner_admission_epoch INTEGER NOT NULL CHECK (owner_admission_epoch > 0),
            covered_seq INTEGER NOT NULL CHECK (covered_seq >= 0),
            injected_seq INTEGER NOT NULL CHECK (injected_seq >= 0 AND injected_seq <= covered_seq),
            input_id TEXT REFERENCES native_runtime_inputs(input_id),
            claim_id TEXT,
            stage TEXT CHECK (stage IN ('triage','full')),
            session_id TEXT,
            request_generation INTEGER,
            CHECK ((injected_seq=0 AND input_id IS NULL AND claim_id IS NULL
                AND stage IS NULL AND session_id IS NULL AND request_generation IS NULL)
                OR (injected_seq>0 AND input_id IS NOT NULL AND claim_id IS NOT NULL
                AND stage IS NOT NULL AND session_id IS NOT NULL AND request_generation>0)),
            PRIMARY KEY(wire_root_id,recipient_lookup,owner_generation,owner_admission_epoch)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "native_runtime_cursor_update_guard",
        """CREATE TRIGGER native_runtime_cursor_update_guard
        BEFORE UPDATE ON native_runtime_source_cursors
        WHEN NEW.wire_root_id IS NOT OLD.wire_root_id
            OR NEW.recipient_lookup IS NOT OLD.recipient_lookup
            OR NEW.owner_thread IS NOT OLD.owner_thread
            OR NEW.owner_generation IS NOT OLD.owner_generation
            OR NEW.owner_admission_epoch IS NOT OLD.owner_admission_epoch
            OR NEW.covered_seq < OLD.covered_seq
            OR NEW.injected_seq < OLD.injected_seq
            OR (NEW.injected_seq = OLD.injected_seq AND NEW.input_id IS NOT OLD.input_id)
        BEGIN SELECT RAISE(ABORT,'native source cursor cannot regress'); END""",
    ),
    (
        "native_runtime_cursor_delete_guard",
        """CREATE TRIGGER native_runtime_cursor_delete_guard
        BEFORE DELETE ON native_runtime_source_cursors
        BEGIN SELECT RAISE(ABORT,'native source cursor cannot be deleted'); END""",
    ),
    (
        "native_runtime_input_identity_guard",
        """CREATE TRIGGER native_runtime_input_identity_guard
        BEFORE UPDATE ON native_runtime_inputs
        WHEN NEW.input_id IS NOT OLD.input_id OR NEW.stage IS NOT OLD.stage
            OR NEW.claim_id IS NOT OLD.claim_id
            OR NEW.execution_id IS NOT OLD.execution_id
            OR NEW.attempt_ordinal IS NOT OLD.attempt_ordinal
            OR NEW.owner_lookup IS NOT OLD.owner_lookup
            OR NEW.owner_thread IS NOT OLD.owner_thread
            OR NEW.owner_generation IS NOT OLD.owner_generation
            OR NEW.owner_token_digest IS NOT OLD.owner_token_digest
            OR OLD.session_id IS NOT NULL
            OR NEW.session_id IS NULL
        BEGIN SELECT RAISE(ABORT,'native runtime input identity is frozen'); END""",
    ),
    (
        "native_runtime_input_delete_guard",
        """CREATE TRIGGER native_runtime_input_delete_guard
        BEFORE DELETE ON native_runtime_inputs
        BEGIN SELECT RAISE(ABORT,'native runtime input cannot be deleted'); END""",
    ),
    (
        "native_runtime_schema_update_guard",
        """CREATE TRIGGER native_runtime_schema_update_guard
        BEFORE UPDATE ON native_runtime_schema_meta
        BEGIN SELECT RAISE(ABORT,'native runtime schema is frozen'); END""",
    ),
    (
        "native_runtime_schema_delete_guard",
        """CREATE TRIGGER native_runtime_schema_delete_guard
        BEFORE DELETE ON native_runtime_schema_meta
        BEGIN SELECT RAISE(ABORT,'native runtime schema is frozen'); END""",
    ),
)
_DDL_DIGEST = hashlib.sha256(json.dumps(_DDL, separators=(",", ":")).encode()).hexdigest()


def assert_native_runtime_schema(db: sqlite3.Connection) -> None:
    try:
        row = db.execute(
            "SELECT version,ddl_digest FROM native_runtime_schema_meta WHERE singleton=1"
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise PublicationActivationBlocked("native runtime schema is not installed") from error
    if row is None or tuple(row) != (2, _DDL_DIGEST):
        raise PublicationActivationBlocked("native runtime schema version differs")
    actual = {
        row["name"]: row["sql"]
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
            "AND name LIKE 'native_runtime_%'"
        )
    }
    if actual != dict(_DDL) or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise PublicationActivationBlocked("native runtime schema has drifted")


def install_native_runtime_schema(store: MutationStore) -> None:
    """Manual fresh-root install only; never called by ordinary startup."""
    if type(store) is not MutationStore:
        raise TypeError("native runtime requires the actual coordinator store")
    with store._transaction() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='native_runtime_schema_meta'"
        ).fetchone()
        if exists is None:
            for _, statement in _DDL:
                db.execute(statement)
            db.execute("INSERT INTO native_runtime_schema_meta VALUES(1,2,?)", (_DDL_DIGEST,))
        assert_native_runtime_schema(db)
