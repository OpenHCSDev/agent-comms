-- Retired schema from the retained D22 journal; converter boundary fixture.
CREATE TABLE enrolled_private_sessions (
                    session_file TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    header_sha256 TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    owner_created_at TEXT NOT NULL,
                    owner_lookup TEXT NOT NULL,
                    owner_generation INTEGER NOT NULL,
                    admission_epoch INTEGER NOT NULL,
                    creator_pid INTEGER NOT NULL
                );
CREATE TABLE operations (
                    commit_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('intent','unknown','committed','refused','aborted-no-write')),
                    evidence_json TEXT
                );
CREATE TABLE private_raw_inputs (
                    input_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'unknown')
                );
CREATE TABLE publications (
                    commit_id TEXT PRIMARY KEY REFERENCES operations(commit_id),
                    session_file TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','observed'))
                );
CREATE TABLE selected_summary_attempts (
                    operation_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('reserved','unknown','linked','manual_committed','declined-prestart','refused','retired_refusal')),
                    commit_id TEXT,
                    decline_reason TEXT
                );
