"""Derive continued-session coverage from existing native and input authorities.

This creates no enrollment or recovery record. A visible session alone cannot
cover history: each user entry needs a recorded native start or verified native
context evidence. All UNKNOWN rows remain unchanged.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .coordinated_runtime_schema import assert_native_runtime_schema
from .input_disposition import InputDocument
from .native_entries import NativeEntry
from .native_pi import NativeContextProof
from .native_runtime_input import NativeRuntimeInput
from .native_session_reopen import NativeSessionIdentity
from .pi_payloads import TextContent
from .private_sidecar import native_request_digest
from .private_path import PrivateFileRole
from .selected_source import SelectedSource, SessionRevision


def verify_continued_private_session(
    root: Path,
    session: Path,
    source: SelectedSource,
    raw_ids: frozenset[str],
    inputs: InputDocument,
    *,
    journal_db: sqlite3.Connection,
) -> None:
    """Reprove complete saved user history; never promote an unresolved attempt."""
    before = SessionRevision.observe(str(session))
    if not before.matches(source.reserved_revision):
        raise ValueError("Continued private source identity changed")
    with NativeEntry.open_evidence(session) as evidence:
        header, entries = evidence.observe()
        if header.version != 3:
            raise ValueError("Continued private session needs a strict native header")
        identity = NativeSessionIdentity(header.id, str(session))
        tracked = NativeEntry.tracked_users(entries)
        covered = evidence.covered_prefix(entries, journal_db)
        required = tuple(entry for entry in entries if entry.id not in covered)
        rows = inputs.rows
        # The locked document already excludes proven process-local future inputs.
        # Every other unresolved owner input except the exact original remains a stop.
        # Do not use admission rollover to hide uncertain history.
        if any(
            row.matches_owner(source.incarnation) and row.unresolved
            and key not in source.pending_input_keys
            for key, row in rows.items()
        ):
            raise ValueError("Continued private history contains unresolved owner input")
        started = {}
        for row in rows.values():
            if row.matches_owner(source.incarnation) and row.has_started:
                native_id = row.native_id
                if native_id in started:
                    raise ValueError("Continued private native start is ambiguous")
                started[native_id] = row
        recorded = (
            _recorded_private_contexts(root, identity)
            if raw_ids or (root / "coordination.sqlite3").exists()
            else {}
        )
        retained = (
            NativeContextProof.read_history_evidence(session, header, required)
            if NativeEntry.tracked_users(required).keys() - (started.keys() | recorded.keys())
            else {}
        )
        observed = set()
        for entry in entries:
            if not entry.is_message or not entry.message.user:
                continue
            native_id = entry.input_id
            # A verified original summary covers replaced source, never raw
            # UNKNOWN markers: those still need their own live context below.
            if entry.id in covered and native_id not in raw_ids:
                continue
            if native_id is None or native_id not in tracked:
                raise ValueError("Continued private user history lacks unique tracked input")
            message = entry.message
            observed.add(native_id)
            started_row = started.get(native_id)
            if started_row is not None:
                text = started_row.sent_text
                if message.content != (
                    TextContent(text),
                ) or message.input_digest != native_request_digest(text):
                    raise ValueError("Continued private user differs from recorded native start")
            elif native_id in recorded:
                proof = recorded[native_id]
                if proof.session_entry_id != entry.id or proof != NativeContextProof.read_evidence(
                    session, native_id, request_generation=proof.request_generation, evidence=evidence
                ):
                    raise ValueError("Continued private user differs from live-recorded context")
            elif native_id not in retained:
                raise ValueError("Continued private user has no verified retained context")
        if not (observed or covered) or not raw_ids.issubset(observed & recorded.keys()):
            raise ValueError("Continued private raw input remains UNKNOWN")
        if not source.reserved_revision.current(str(session)):
            raise ValueError("Continued private history changed during coverage check")


def _recorded_private_contexts(
    root: Path, session: NativeSessionIdentity
) -> dict[str, NativeContextProof]:
    """Read immutable live-result columns; journal parsing never supplies them."""
    database = root / "coordination.sqlite3"
    # Read-only URI cannot create a missing coordinator or install its schema.
    info = database.lstat()
    PrivateFileRole.require(info)
    if database != database.resolve(strict=True) or info.st_nlink != 1:
        raise ValueError("Continued private coordinator is not canonical private storage")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        assert_native_runtime_schema(db)
        return NativeRuntimeInput.recorded_contexts(db, session)
