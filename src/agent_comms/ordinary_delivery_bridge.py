"""Ordinary-delivery N/K candidate bridge: record-only, never authority.

Ordinary bus sends can be recorded as versioned delivery *candidates* for
committed private participants. A candidate row is NOT a sealed coordinator
receipt, NOT a selected claim, and NOT a wake: no model is launched, no
`wake_claims` row exists, and no injection cursor advances. The recorded
identity is always re-read from the committed bus row; caller-stamped labels
are never trusted. Wiring the public `comms_send` tool into this recorder
stays a separate, test-gated integration decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_cohort import _assert_schema as assert_cohort_schema
from .coordination_store import IdentityConflict, MutationStore

if TYPE_CHECKING:
    from .operations import Comms

from .private_sidecar import (
    create_sidecar_file,
    sidecar_connection,
)

_DDL = (
    (
        "ordinary_delivery_meta",
        """CREATE TABLE ordinary_delivery_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL CHECK (version = 1),
            ddl_digest TEXT NOT NULL CHECK (length(ddl_digest) = 64)
        ) STRICT""",
    ),
    (
        "ordinary_delivery_candidates",
        """CREATE TABLE ordinary_delivery_candidates (
            wire_root_id TEXT NOT NULL CHECK (
                length(wire_root_id) = 32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'),
            wire_seq INTEGER NOT NULL CHECK (wire_seq > 0),
            message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 256),
            exact_target TEXT NOT NULL CHECK (length(exact_target) BETWEEN 1 AND 256),
            sender TEXT NOT NULL CHECK (length(sender) BETWEEN 1 AND 256),
            envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            PRIMARY KEY (wire_root_id, wire_seq)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "ordinary_delivery_recipients",
        """CREATE TABLE ordinary_delivery_recipients (
            wire_root_id TEXT NOT NULL,
            wire_seq INTEGER NOT NULL,
            recipient_lookup TEXT NOT NULL CHECK (
                length(recipient_lookup) = 32 AND recipient_lookup NOT GLOB '*[^0-9a-f]*'),
            canonical_thread TEXT NOT NULL CHECK (length(canonical_thread) BETWEEN 1 AND 256),
            PRIMARY KEY (wire_root_id, wire_seq, recipient_lookup)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "ordinary_delivery_update_guard",
        """CREATE TRIGGER ordinary_delivery_update_guard
        BEFORE UPDATE ON ordinary_delivery_candidates
        BEGIN SELECT RAISE(ABORT,'a recorded ordinary delivery is immutable'); END""",
    ),
    (
        "ordinary_delivery_delete_guard",
        """CREATE TRIGGER ordinary_delivery_delete_guard
        BEFORE DELETE ON ordinary_delivery_candidates
        BEGIN SELECT RAISE(ABORT,'a recorded ordinary delivery cannot be deleted'); END""",
    ),
    (
        "ordinary_delivery_meta_update_guard",
        """CREATE TRIGGER ordinary_delivery_meta_update_guard
        BEFORE UPDATE ON ordinary_delivery_meta
        BEGIN SELECT RAISE(ABORT,'ordinary delivery schema is frozen'); END""",
    ),
    (
        "ordinary_delivery_meta_delete_guard",
        """CREATE TRIGGER ordinary_delivery_meta_delete_guard
        BEFORE DELETE ON ordinary_delivery_meta
        BEGIN SELECT RAISE(ABORT,'ordinary delivery schema is frozen'); END""",
    ),
)
_DDL_DIGEST = hashlib.sha256(json.dumps(_DDL, separators=(",", ":")).encode()).hexdigest()

_STORE_NAME = "ordinary_delivery.sqlite3"


def ordinary_delivery_store_path(store: MutationStore) -> Path:
    if type(store) is not MutationStore:
        raise TypeError("ordinary delivery requires the actual coordinator store")
    return (store.path.parent / _STORE_NAME).absolute()


@dataclass(frozen=True, slots=True)
class OrdinaryDeliveryCandidate:
    wire_root_id: str
    wire_seq: int
    message_id: str
    exact_target: str
    sender: str
    envelope_digest: str
    recorded_at_ms: int
    recipients: tuple[str, ...]


def install_ordinary_delivery_schema(store: MutationStore) -> None:
    """Explicit fresh-root install; ordinary callers may rely on _ensure."""
    _ensure_ordinary_delivery_schema(store)


def _ensure_ordinary_delivery_schema(store: MutationStore) -> None:
    create_sidecar_file(ordinary_delivery_store_path(store), _DDL, _DDL_DIGEST)


def _committed_bus_row(comms: Comms, seq: int) -> dict:
    """Re-read the trusted identity from the committed public bus row itself."""
    page = comms.bus.full_history_page(after=seq - 1, limit=1, max_bytes=1024 * 1024)
    if not page.messages or page.messages[0].seq != seq:
        raise IdentityConflict("ordinary delivery requires the committed public bus row")
    record = page.messages[0].to_wire()
    record.pop("seq", None)
    return {
        "message_id": str(record["id"]),
        "exact_target": str(record["to"]),
        "sender": str(record["from"]),
        "envelope_digest": hashlib.sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def record_ordinary_delivery(
    store: MutationStore,
    comms: Comms,
    *,
    wire_root_id: str,
    wire_seq: int,
    recipient_lookups: tuple[str, ...],
) -> OrdinaryDeliveryCandidate:
    """Record one committed bus row as a delivery candidate for committed participants.

    Idempotent only for the exact same identity; a differing envelope or
    recipient set for an already-recorded row is a conflict. No wake, claim,
    sealed receipt, or cursor is produced here.
    """
    if (
        type(store) is not MutationStore
        or type(wire_root_id) is not str
        or len(wire_root_id) != 32
        or any(character not in "0123456789abcdef" for character in wire_root_id)
        or type(wire_seq) is not int
        or wire_seq <= 0
        or not recipient_lookups
        or any(type(lookup) is not str or len(lookup) != 32 for lookup in recipient_lookups)
    ):
        raise ValueError("ordinary delivery recording requires bounded exact identities")
    # Hold the coordination store WRITE transaction across the participant
    # guard and the sidecar insert so a concurrent participant mutation
    # (commit/rename/owner change) cannot interleave (same TOCTOU fence as
    # the prompt binding).
    with store._transaction() as db:
        assert_cohort_schema(db)
        committed = []
        for lookup in dict.fromkeys(recipient_lookups):
            participant = store._participant(lookup)
            if not participant.committed:
                raise IdentityConflict("ordinary delivery requires committed participants")
            committed.append((lookup, participant.owner_thread))
        row = _committed_bus_row(comms, wire_seq)
        _ensure_ordinary_delivery_schema(store)
        now = store._now(0)
        with sidecar_connection(ordinary_delivery_store_path(store), _DDL, _DDL_DIGEST) as sidecar:
            existing = sidecar.execute(
                "SELECT * FROM ordinary_delivery_candidates " "WHERE wire_root_id=? AND wire_seq=?",
                (wire_root_id, wire_seq),
            ).fetchone()
            if existing is not None:
                if (
                    existing["message_id"] != row["message_id"]
                    or existing["exact_target"] != row["exact_target"]
                    or existing["sender"] != row["sender"]
                    or existing["envelope_digest"] != row["envelope_digest"]
                ):
                    raise IdentityConflict("recorded ordinary delivery identity differs")
                recorded_recipients = tuple(
                    rec["recipient_lookup"]
                    for rec in sidecar.execute(
                        "SELECT recipient_lookup FROM ordinary_delivery_recipients "
                        "WHERE wire_root_id=? AND wire_seq=? ORDER BY recipient_lookup",
                        (wire_root_id, wire_seq),
                    )
                )
                if set(recorded_recipients) != set(recipient_lookups):
                    raise IdentityConflict("recorded ordinary delivery recipients differ")
                return OrdinaryDeliveryCandidate(
                    wire_root_id,
                    wire_seq,
                    existing["message_id"],
                    existing["exact_target"],
                    existing["sender"],
                    existing["envelope_digest"],
                    existing["recorded_at_ms"],
                    recorded_recipients,
                )
            sidecar.execute("BEGIN IMMEDIATE")
            try:
                sidecar.execute(
                    "INSERT INTO ordinary_delivery_candidates"
                    "(wire_root_id,wire_seq,message_id,exact_target,sender,"
                    "envelope_digest,recorded_at_ms) VALUES (?,?,?,?,?,?,?)",
                    (
                        wire_root_id,
                        wire_seq,
                        row["message_id"],
                        row["exact_target"],
                        row["sender"],
                        row["envelope_digest"],
                        now,
                    ),
                )
                for lookup, thread_name in sorted(committed):
                    sidecar.execute(
                        "INSERT INTO ordinary_delivery_recipients"
                        "(wire_root_id,wire_seq,recipient_lookup,canonical_thread) "
                        "VALUES (?,?,?,?)",
                        (wire_root_id, wire_seq, lookup, thread_name),
                    )
            except BaseException:
                sidecar.execute("ROLLBACK")
                raise
            sidecar.execute("COMMIT")
    return OrdinaryDeliveryCandidate(
        wire_root_id,
        wire_seq,
        row["message_id"],
        row["exact_target"],
        row["sender"],
        row["envelope_digest"],
        now,
        tuple(lookup for lookup, _ in sorted(committed)),
    )


def read_ordinary_delivery_candidates(
    store: MutationStore,
    *,
    recipient_lookup: str,
    after_seq: int = 0,
    limit: int = 100,
) -> tuple[OrdinaryDeliveryCandidate, ...]:
    """Bounded ascending read of candidates delivered to one participant.

    Read-only projection: a candidate row never implies a sealed claim, a
    wake, or an advanced cursor.
    """
    if (
        type(store) is not MutationStore
        or type(recipient_lookup) is not str
        or len(recipient_lookup) != 32
        or type(after_seq) is not int
        or after_seq < 0
        or type(limit) is not int
        or limit < 1
        or limit > 100
    ):
        raise ValueError("ordinary delivery read requires bounded exact identities")
    path = ordinary_delivery_store_path(store)
    if not path.exists():
        return ()
    with sidecar_connection(path, _DDL, _DDL_DIGEST) as db:
        rows = db.execute(
            "SELECT c.* FROM ordinary_delivery_candidates c "
            "JOIN ordinary_delivery_recipients r ON r.wire_root_id=c.wire_root_id "
            "AND r.wire_seq=c.wire_seq "
            "WHERE r.recipient_lookup=? AND c.wire_seq>? "
            "ORDER BY c.wire_seq LIMIT ?",
            (recipient_lookup, after_seq, limit),
        ).fetchall()
    return tuple(
        OrdinaryDeliveryCandidate(
            row["wire_root_id"],
            row["wire_seq"],
            row["message_id"],
            row["exact_target"],
            row["sender"],
            row["envelope_digest"],
            row["recorded_at_ms"],
            (),
        )
        for row in rows
    )
