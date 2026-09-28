"""Bounded, read-only awareness of sealed private work for one selected owner.

The candidate WAL locates changed bus rows. It never proves a claim, response
obligation, native input, or permission to act. The caller supplies the already
verified current original, selected claim, and live owner-turn snapshot; the
native send boundary must recheck that owner after this optional read.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .bus_publication import CommittedInitial, stable_thread_lookup
from .cohort_schema import COHORT_SCHEMA_VERSION, assert_optional_awareness_schema
from .coordination import (
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
    CoordinationError,
    WakeClaim,
)
from .coordination_cohort import _assert_schema, _receipt_matches
from .declarations import (
    RelationViolationError,
    Thread,
    _require_no_private_owner_rename,
    _store_lock,
)
from .registration import Registration
from .wake_candidate_index import ProjectionUnavailableError, WakeCandidateIndex


@dataclass(frozen=True, slots=True)
class OptionalAwarenessResult:
    """A complete bounded context, or an explicit no-supplement result."""

    text: str
    mandatory_complete: bool
    omitted_count: int = 0
    omission_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OptionalAwarenessProjection:
    """Project one stable recipient window without a bus scan or write lock.

    ``after_seq`` and ``through_seq`` must come from the caller's trusted
    current-owner/native-cursor and bus high-water snapshots. Passing zero for
    ``after_seq`` is conservative: it can omit an oversized history, never skip
    a binding decision. The foreground runner still gates the result to 250 ms.
    """

    index: WakeCandidateIndex
    after_seq: int
    through_seq: int
    expected_participant_generation: int
    expected_admission_epoch: int
    max_rows: int = 100
    max_text_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        if (
            type(self.index) is not WakeCandidateIndex
            or type(self.after_seq) is not int
            or self.after_seq < 0
            or type(self.through_seq) is not int
            or self.through_seq <= self.after_seq
            or type(self.expected_participant_generation) is not int
            or self.expected_participant_generation < 1
            or type(self.expected_admission_epoch) is not int
            or self.expected_admission_epoch < 1
            or type(self.max_rows) is not int
            or not 1 <= self.max_rows <= 100
            or type(self.max_text_bytes) is not int
            or not 1 <= self.max_text_bytes <= 16 * 1024
        ):
            raise ValueError("optional awareness requires a bounded trusted source window")

    def __call__(
        self, initial: CommittedInitial, claim: WakeClaim, owner: Thread
    ) -> OptionalAwarenessResult:
        """Return complete SQL-backed rows, or omit the entire supplement.

        No selected decision or open obligation is ranked away. A stale WAL,
        unsealed candidate, different owner, excess row count, or prompt budget
        exhausts the optional path without changing original delivery.
        """
        try:
            return self._build(initial, claim, owner)
        except (
            CoordinationError,
            ProjectionUnavailableError,
            RelationViolationError,
            sqlite3.Error,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            return OptionalAwarenessResult("", False, omission_reason=type(error).__name__)

    def _build(
        self, initial: CommittedInitial, claim: WakeClaim, owner: Thread
    ) -> OptionalAwarenessResult:
        if (
            type(initial) is not CommittedInitial
            or type(claim) is not WakeClaim
            or type(owner) is not Thread
        ):
            raise ValueError("optional awareness requires verified typed source identities")
        if (
            owner.pid != os.getpid()
            or not owner.role.executable
            or owner.active_turn is None
            or owner.active_turn.admission_generation != self.expected_admission_epoch
            or claim.recipient != owner.name
            or claim.recipient_lookup != stable_thread_lookup(owner.created_at)
            or claim.wire_seq != initial.message.seq
            or claim.message_id != initial.message.message_id
            or initial.message.seq <= self.after_seq
            or initial.message.seq > self.through_seq
        ):
            raise ProjectionUnavailableError("selected owner or source window changed")
        _require_no_private_owner_rename(self.index.bus._path.parent)
        # Each selected row must now carry immutable same-transaction owner
        # generation provenance. Legacy rows lacking it omit the whole read.
        root_id = initial.wire_root_id
        lookup = claim.recipient_lookup
        page = self.index.page(
            root_id=root_id,
            recipient_lookup=lookup,
            after_seq=self.after_seq,
            required_through_seq=self.through_seq,
            limit=self.max_rows,
        )
        if page.has_more:
            raise ProjectionUnavailableError("binding candidate decisions exceed the row budget")

        path = self.index.bus._path.with_name("coordination.sqlite3")
        with closing(
            sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=0.05)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=50")
            db.execute("BEGIN")
            self._verify_schema_and_owner(db, initial, claim, owner)
            decisions = self._selected_decisions(db, root_id, lookup, owner.name, page.through_seq)
            candidates = tuple(
                (row.source_seq, row.message_id, row.wake_mode, row.target) for row in page.entries
            )
            accepted = tuple(
                (row["wire_seq"], row["message_id"], row["wake_mode"], row["exact_target"])
                for row in decisions
            )
            if candidates != accepted:
                raise ProjectionUnavailableError("candidate rows differ from sealed decisions")
            obligations = self._open_obligations(db, lookup, owner.name)
            selected = [
                row
                for row in decisions
                if row["accepted_generation"] == self.expected_participant_generation
            ]
            if not any(row["claim_id"] == claim.claim_id for row in selected):
                raise ProjectionUnavailableError("current selected claim has no owner generation")
            current_obligations = [
                row
                for row in obligations
                if row["accepted_generation"] == self.expected_participant_generation
            ]
            historical_omitted = (len(decisions) - len(selected)) + (
                len(obligations) - len(current_obligations)
            )

        # The SQL read above is an immutable snapshot, not a live-owner lease.
        # Do not hold the SQL read lock while consulting the registry. A newer
        # SQL generation or owner turn omits awareness rather than borrowing
        # a stale result; the native send has its own final admission fence.
        self._verify_live_inclusion(path, lookup, owner)
        context = {
            "recipient_lookup": lookup,
            "through_seq": page.through_seq,
            "selected": [
                {
                    "source_seq": row["wire_seq"],
                    "message_id": row["message_id"],
                    "claim_id": row["claim_id"],
                    "wake_mode": row["wake_mode"],
                    "disposition": row["disposition"],
                    "target": row["exact_target"],
                }
                for row in selected
            ],
            "open_obligations": [
                {
                    "execution_id": row["execution_id"],
                    "target": row["exact_target"],
                    "state": row["state"],
                }
                for row in current_obligations
            ],
        }
        text = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(text.encode("utf-8")) > self.max_text_bytes:
            raise ProjectionUnavailableError("binding awareness exceeds the byte budget")
        return OptionalAwarenessResult(text, True, omitted_count=historical_omitted)

    def _verify_live_inclusion(self, path: os.PathLike[str], lookup: str, owner: Thread) -> None:
        _require_no_private_owner_rename(self.index.bus._path.parent)
        registry = Registration(self.index.bus._registry.store.path)
        with _store_lock(registry.store.path, blocking=False):
            snapshot = registry.store._read_unlocked().snapshot()
            actual = snapshot.threads.get(owner.name)
            status = snapshot.statuses.get(owner.name)
            if (
                actual is None
                or status is None
                or not status.active
                or snapshot.admission_generations.get(owner.name) != self.expected_admission_epoch
                or (
                    actual.name,
                    actual.created_at,
                    actual.pid,
                    actual.role,
                    actual.worktree,
                    actual.active_turn,
                    actual.goal,
                )
                != (
                    owner.name,
                    owner.created_at,
                    owner.pid,
                    owner.role,
                    owner.worktree,
                    owner.active_turn,
                    owner.goal,
                )
            ):
                raise ProjectionUnavailableError("current registry owner turn changed")
        # A new read transaction, not the earlier candidate/decision snapshot,
        # observes a supported SQL generation advance during the optional read.
        with closing(
            sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True, timeout=0)
        ) as db:
            row = db.execute(
                "SELECT g.generation,p.committed,g.owner_thread FROM owner_generations g "
                "JOIN participants p ON p.participant_lookup=g.owner_lookup "
                "WHERE g.owner_lookup=?",
                (lookup,),
            ).fetchone()
            if row != (self.expected_participant_generation, 1, owner.name):
                raise ProjectionUnavailableError("current participant generation changed")
        _require_no_private_owner_rename(self.index.bus._path.parent)

    def _verify_schema_and_owner(
        self, db: sqlite3.Connection, initial: CommittedInitial, claim: WakeClaim, owner: Thread
    ) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        meta = db.execute(
            "SELECT schema_version,snapshot_version FROM schema_meta WHERE singleton=1"
        ).fetchone()
        if (
            version != COORDINATION_SCHEMA_VERSION
            or meta is None
            or tuple(meta)
            != (
                COORDINATION_SCHEMA_VERSION,
                COORDINATION_SNAPSHOT_VERSION,
            )
        ):
            raise ProjectionUnavailableError("coordinator schema changed")
        _assert_schema(db)
        assert_optional_awareness_schema(db)
        cohort_version = db.execute(
            "SELECT version FROM cohort_schema_meta WHERE singleton=1"
        ).fetchone()
        if cohort_version is None or cohort_version[0] != COHORT_SCHEMA_VERSION:
            raise ProjectionUnavailableError("cohort schema changed")
        person = db.execute(
            "SELECT p.committed,g.owner_thread,g.generation FROM participants p "
            "JOIN owner_generations g ON g.owner_lookup=p.participant_lookup "
            "WHERE p.participant_lookup=?",
            (claim.recipient_lookup,),
        ).fetchone()
        if (
            person is None
            or person["committed"] != 1
            or person["owner_thread"] != owner.name
            or person["generation"] != self.expected_participant_generation
        ):
            raise ProjectionUnavailableError("selected participant owner changed")
        receipt = _receipt_matches(db, initial)
        if not any(current == claim for current in receipt.claims):
            raise ProjectionUnavailableError("current selected claim lost its sealed receipt")

    def _selected_decisions(
        self, db: sqlite3.Connection, root_id: str, lookup: str, owner_name: str, through_seq: int
    ) -> list[sqlite3.Row]:
        rows = db.execute(
            "SELECT c.claim_id,c.recipient,c.wire_seq,c.message_id,c.wake_mode,"
            "c.disposition,r.exact_target,r.sealed,r.message_id AS receipt_message_id,"
            "m.claim_id AS member_claim,"
            "d.claim_id AS delivery_claim,d.kind,d.canonical_thread,"
            "ag.claim_id AS generation_claim,ag.recipient_lookup AS generation_lookup,"
            "ag.canonical_thread AS generation_thread,"
            "ag.owner_generation AS accepted_generation "
            "FROM wake_claims c "
            "LEFT JOIN claim_batch_receipts r ON r.wire_root_id=? AND r.wire_seq=c.wire_seq "
            "LEFT JOIN claim_batch_members m ON m.wire_root_id=r.wire_root_id "
            "AND m.wire_seq=r.wire_seq AND m.claim_id=c.claim_id "
            "LEFT JOIN cohort_delivery_receipts d ON d.wire_root_id=r.wire_root_id "
            "AND d.wire_seq=r.wire_seq AND d.recipient_lookup=c.recipient_lookup "
            "LEFT JOIN awareness_claim_generations ag ON ag.claim_id=c.claim_id "
            "AND ag.wire_root_id=r.wire_root_id AND ag.wire_seq=r.wire_seq "
            "WHERE c.recipient_lookup=? AND c.wire_seq>? AND c.wire_seq<=? "
            "ORDER BY c.wire_seq LIMIT ?",
            (root_id, lookup, self.after_seq, through_seq, self.max_rows + 1),
        ).fetchall()
        if len(rows) > self.max_rows or any(
            row["sealed"] != 1
            or row["receipt_message_id"] != row["message_id"]
            or row["member_claim"] != row["claim_id"]
            or row["delivery_claim"] != row["claim_id"]
            or row["kind"] != "selected"
            or row["canonical_thread"] != row["recipient"]
            or row["generation_claim"] != row["claim_id"]
            or row["generation_lookup"] != lookup
            or row["generation_thread"] != row["recipient"]
            or type(row["accepted_generation"]) is not int
            or row["accepted_generation"] > self.expected_participant_generation
            or (
                row["accepted_generation"] == self.expected_participant_generation
                and row["recipient"] != owner_name
            )
            for row in rows
        ):
            raise ProjectionUnavailableError("binding decisions are unsealed or over budget")
        return rows

    def _open_obligations(
        self, db: sqlite3.Connection, lookup: str, owner_name: str
    ) -> list[sqlite3.Row]:
        rows = db.execute(
            "SELECT o.execution_id,o.exact_target,o.state,e.owner_thread,e.origin,"
            "ec.claim_id AS linked_claim,ag.owner_generation AS accepted_generation,"
            "ag.canonical_thread AS generation_thread "
            "FROM obligations o JOIN executions e ON e.execution_id=o.execution_id "
            "LEFT JOIN execution_claims ec ON ec.execution_id=e.execution_id "
            "LEFT JOIN awareness_claim_generations ag ON ag.claim_id=ec.claim_id "
            "WHERE e.owner_lookup=? AND o.state IN ('pending','publishing','deferred') "
            "ORDER BY o.created_at_ms,o.execution_id LIMIT ?",
            (lookup, self.max_rows + 1),
        ).fetchall()
        if len(rows) > self.max_rows or len({row["execution_id"] for row in rows}) != len(rows):
            raise ProjectionUnavailableError("open obligations exceed complete row budget")
        if any(
            row["origin"] != "wire"
            or row["linked_claim"] is None
            or type(row["accepted_generation"]) is not int
            or row["accepted_generation"] > self.expected_participant_generation
            or row["generation_thread"] != row["owner_thread"]
            or (
                row["accepted_generation"] == self.expected_participant_generation
                and row["owner_thread"] != owner_name
            )
            for row in rows
        ):
            raise ProjectionUnavailableError("open obligation lacks exact owner provenance")
        return rows
