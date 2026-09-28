"""Derive continued-session coverage from existing native and input authorities.

This creates no enrollment or recovery record. A visible session alone cannot
cover history: each user entry needs an independently recorded native start or
live-recorded coordinator proof. All UNKNOWN rows remain unchanged.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .backend import _session_revision
from .native_runtime_input import NativeRuntimeInput
from .coordinated_runtime_schema import assert_native_runtime_schema
from .input_disposition import InputDispositions
from .native_entries import NativeEntry
from .native_pi import NativeContextProof
from .pi_payloads import TextContent
from .private_sidecar import native_request_digest


def verify_continued_private_session(
    root: Path, session: Path, source: dict, raw_ids: frozenset[str]
) -> None:
    """Reprove complete saved user history; never promote an unresolved attempt."""
    before = _session_revision(str(session))
    owner = source.get("ownerName")
    if (
        before is None
        or source.get("reservedRevision") != json.loads(json.dumps(before))
        or type(owner) is not str
        or not owner
        or session.parent.parent != (root / "native-sessions").resolve(strict=True)
    ):
        raise ValueError("Continued private source identity changed")
    header, entries = NativeEntry.read_evidence(session)
    if header.version != 3:
        raise ValueError("Continued private session needs a strict native header")
    tracked = NativeEntry.tracked_users(entries)
    rows = InputDispositions(root / InputDispositions.filename).read().rows
    # Any unresolved owner input except the exact new original remains a stop.
    # Do not use admission rollover to hide uncertain history.
    if any(
        row.owner == owner and row.unresolved and key != source.get("ingressKey")
        for key, row in rows.items()
    ):
        raise ValueError("Continued private history contains unresolved owner input")
    started = {}
    for row in rows.values():
        if row.owner == owner and not row.unresolved:
            native_id = row.native_id
            if native_id in started:
                raise ValueError("Continued private native start is ambiguous")
            started[native_id] = row
    recorded = (
        _recorded_private_contexts(root, session, owner)
        if raw_ids or (root / "coordination.sqlite3").exists()
        else {}
    )
    observed = set()
    for entry in entries:
        if not entry.is_message or not entry.message.user:
            continue
        native_id = entry.input_id
        if native_id is None or native_id not in tracked:
            raise ValueError("Continued private user history lacks unique tracked input")
        message = entry.message
        observed.add(native_id)
        started_row = started.get(native_id)
        if started_row is not None:
            text = started_row.sent_text
            if (
                type(text) is not str
                or not started_row.turn_id
                or message.content != (TextContent(text),)
                or message.input_digest != native_request_digest(text)
            ):
                raise ValueError("Continued private user differs from recorded native start")
        elif native_id in recorded:
            proof = recorded[native_id]
            if proof.session_entry_id != entry.id or proof != NativeContextProof.read_evidence(
                session, native_id, request_generation=proof.request_generation
            ):
                raise ValueError("Continued private user differs from live-recorded context")
        else:
            raise ValueError("Continued private user has no independent start evidence")
    if not observed or not raw_ids.issubset(observed & recorded.keys()):
        raise ValueError("Continued private raw input remains UNKNOWN")
    if _session_revision(str(session)) != before:
        raise ValueError("Continued private history changed during coverage check")


def _recorded_private_contexts(
    root: Path, session: Path, owner: str
) -> dict[str, NativeContextProof]:
    """Read immutable live-result columns; journal parsing never supplies them."""
    database = root / "coordination.sqlite3"
    # Read-only URI cannot create a missing coordinator or install its schema.
    info = database.lstat()
    if (
        database != database.resolve(strict=True)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Continued private coordinator is not canonical private storage")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        assert_native_runtime_schema(db)
        rows = NativeRuntimeInput.select(
            db, where="owner_lookup=?", parameters=(session.parent.name,)
        )
    if any(row.session_id is None for row in rows):
        raise ValueError("Continued private owner has unresolved native input")
    return {
        row.input_id: NativeContextProof(
            row.input_id,
            row.session_id,
            row.session_entry_id,
            row.request_generation,
            row.llm_context_digest,
            session,
        )
        for row in rows
        if row.session_file == str(session) and row.owner_thread == owner
    }
