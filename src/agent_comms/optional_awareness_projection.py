"""Bounded, read-only awareness of sealed private work for one selected owner.

The candidate WAL locates changed bus rows. It never proves a claim, response
obligation, native input, or permission to act. The caller supplies the already
verified current original, selected claim, and live owner-turn snapshot; the
native send boundary must recheck that owner after this optional read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationError
from agent_comms.coordination_schema import (
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
)
from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink, WakeAssignment
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import OwnerGenerations
from agent_comms.coordination_tables.responses import ResponseObligation

from .audience_manifest import FrozenRecipient
from .bus_publication import CommittedDelivery, stable_thread_lookup
from .child_process import ProcessIdentity
from .cohort_schema import (
    COHORT_SCHEMA_VERSION,
    AwarenessClaimGenerations,
    ClaimBatchMembers,
    ClaimBatchReceipts,
    CohortDeliveryReceipts,
    assert_cohort_schema,
    assert_optional_awareness_schema,
)
from .coordination_cohort import _receipt_matches
from .errors import RelationViolationError
from .native_input_owner import RegistryOwner
from .private_registry_guard import _require_no_private_owner_rename
from .registration import Registration
from .store_files import _store_lock
from .threads import Thread
from .typed_table import SQLiteUserVersion, TypedRow
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .turn_context import ContextSegment, InstructionFile, ResourceProvenance, WireProvenance
from .wake_candidate_index import ProjectionUnavailableError, WakeCandidateIndex
from .context_segments.optional_awareness import (
    _SelectedDecision, _OpenObligation, OptionalAwarenessResult, OmittedAwareness, CompleteAwareness,
)


@dataclass(frozen=True)
class _ParticipantOwner(TypedRow):
    generation: int
    committed: bool
    owner_thread: str


@dataclass(frozen=True)
class _CoordinatorVersions(TypedRow):
    schema_version: int
    snapshot_version: int


@dataclass(frozen=True)
class _CohortVersion(TypedRow):
    version: int


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
    expected_admission_generation: int
    max_rows: int = 100
    max_text_bytes: int = 16 * 1024
    build_seconds: ClassVar[float] = 0.25
    # Completion alone releases the single reader. Timed-out reads must not
    # queue a daemon fleet or occupy the default executor used by native work.
    _build_slot: ClassVar[threading.BoundedSemaphore] = threading.BoundedSemaphore(1)

    @classmethod
    def for_selected(
        cls,
        index: WakeCandidateIndex,
        *,
        through_seq: int,
        generation: int,
        admission_generation: int,
    ) -> OptionalAwarenessProjection:
        with index.bus.log.locked():
            after_seq = index.bus.log._private_marker_unlocked().admission_after_seq
        return cls(index, after_seq, through_seq, generation, admission_generation)

    @staticmethod
    def _deliver(
        finished: asyncio.Future[OptionalAwarenessResult], result: OptionalAwarenessResult
    ):
        if not finished.done():
            finished.set_result(result)

    def _finish_read(self, initial, assignment, owner, loop, finished) -> None:
        try:
            result = self(initial, assignment, owner)
        except Exception as error:
            result = OmittedAwareness(type(error).__name__)
        finally:
            self._build_slot.release()
        with suppress(RuntimeError):  # The caller's loop may have closed after timeout.
            loop.call_soon_threadsafe(self._deliver, finished, result)

    async def segments(
        self,
        initial: CommittedDelivery,
        assignment: WakeAssignment,
        owner: Thread,
    ) -> tuple[ContextSegment, ...]:
        deadline = time.monotonic() + self.build_seconds
        if not self._build_slot.acquire(blocking=False):
            return OmittedAwareness("builder busy").segments(self.max_text_bytes)
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[OptionalAwarenessResult] = loop.create_future()
        try:
            try:
                threading.Thread(
                    target=self._finish_read,
                    args=(initial, assignment, owner, loop, finished),
                    name="agent-comms-optional-awareness",
                    daemon=True,
                ).start()
            except RuntimeError:
                self._build_slot.release()
                raise
            result = await asyncio.wait_for(finished, max(0.0, deadline - time.monotonic()))
            segments = result.segments(self.max_text_bytes)
            if time.monotonic() > deadline:
                raise TimeoutError("optional awareness exceeded the build deadline")
            return segments
        except Exception as error:
            return OmittedAwareness(type(error).__name__).segments(self.max_text_bytes)

    def __post_init__(self) -> None:
        # These are typed internal snapshots. External rows are decoded by
        # TypedTable/FieldCodec once, where they enter below.
        if not 0 <= self.after_seq < self.through_seq:
            raise ValueError("optional awareness requires a bounded trusted source window")
        if min(self.expected_participant_generation, self.expected_admission_generation) < 1:
            raise ValueError("optional awareness requires positive captured generations")
        if not 1 <= self.max_rows <= 100:
            raise ValueError("optional awareness row budget is outside the bounded read")
        if not 1 <= self.max_text_bytes <= 16 * 1024:
            raise ValueError("optional awareness text budget is outside the bounded read")

    def __call__(
        self, initial: CommittedDelivery, assignment: WakeAssignment, owner: Thread
    ) -> OptionalAwarenessResult:
        """Return complete SQL-backed rows, or omit the entire supplement.

        No selected decision or open obligation is ranked away. A stale WAL,
        unsealed candidate, different owner, excess row count, or resource budget
        exhausts the optional path without changing original delivery.
        """
        try:
            return self._build(initial, assignment, owner)
        except (
            CoordinationError,
            ProjectionUnavailableError,
            RelationViolationError,
            sqlite3.Error,
            OSError,
            TypeError,
            ValueError,
            KeyError,
        ) as error:
            return OmittedAwareness(type(error).__name__)

    def _build(
        self, initial: CommittedDelivery, assignment: WakeAssignment, owner: Thread
    ) -> OptionalAwarenessResult:
        if owner.process_identity != ProcessIdentity.capture(os.getpid()):
            raise ProjectionUnavailableError("selected owner is not this process")
        if not owner.role.executable:
            raise ProjectionUnavailableError("selected owner is not executable")
        turn = owner.active_turn
        if turn is None or turn.admission_generation != self.expected_admission_generation:
            raise ProjectionUnavailableError("selected owner turn changed")
        if FrozenRecipient(assignment.recipient_lookup, assignment.recipient) != FrozenRecipient(
            stable_thread_lookup(owner.created_at),
            owner.name,
        ):
            raise ProjectionUnavailableError("selected recipient changed")
        if (assignment.wire_seq, assignment.message_id) != (
            initial.message.seq,
            initial.message.message_id,
        ):
            raise ProjectionUnavailableError("selected source changed")
        if not self.after_seq < initial.message.seq <= self.through_seq:
            raise ProjectionUnavailableError("selected source is outside the captured window")
        _require_no_private_owner_rename(self.index.bus.log.path.parent)
        # Each selected row must now carry immutable same-transaction owner
        # generation provenance. An incomplete join omits the whole read.
        root_id = initial.wire_root_id
        lookup = assignment.recipient_lookup
        page = self.index.page(
            root_id=root_id,
            recipient_lookup=lookup,
            after_seq=self.after_seq,
            required_through_seq=self.through_seq,
            limit=self.max_rows,
        )
        if page.has_more:
            raise ProjectionUnavailableError("binding candidate decisions exceed the row budget")

        path = self.index.bus.log.path.with_name("coordination.sqlite3")
        with CoordinationStore.observing(path, lock_timeout=0.05) as db:
            self._verify_schema_and_owner(db, initial, assignment, owner)
            decisions = self._selected_decisions(db, root_id, lookup, page.through_seq)
            candidates = tuple(
                (row.source_seq, row.message_id, row.wake_mode, row.target) for row in page.entries
            )
            accepted = tuple(row.candidate for row in decisions)
            if candidates != accepted:
                raise ProjectionUnavailableError("candidate rows differ from sealed decisions")
            obligations = self._open_obligations(db, lookup)
            expected = OwnerGenerations(
                owner_lookup=lookup,
                owner_thread=owner.name,
                generation=self.expected_participant_generation,
            )
            selected = [row for row in decisions if row.generation.current(expected)]
            if not any(
                row.assignment.assignment_id == assignment.assignment_id for row in selected
            ):
                raise ProjectionUnavailableError("current selected claim has no owner generation")
            current_obligations = [row for row in obligations if row.generation.current(expected)]
            historical_omitted = (len(decisions) - len(selected)) + (
                len(obligations) - len(current_obligations)
            )

        # The SQL read above is an immutable snapshot, not a live-owner lease.
        # Do not hold the SQL read lock while consulting the registry. A newer
        # SQL generation or owner turn omits awareness rather than borrowing
        # a stale result; the native send has its own final admission fence.
        self._verify_live_inclusion(path, lookup, owner)
        instruction = InstructionFile.read("selected-awareness.md")
        content_instruction = InstructionFile.read("selected-awareness-content.md")
        # This digest names the captured typed SQL projection, not the whole file
        # or a refreshed permission. The original rows remain in their owner.
        digest = hashlib.sha256(
            json.dumps(
                FieldCodec.encode((tuple(selected), tuple(current_obligations))), sort_keys=True
            ).encode()
        ).hexdigest()
        result = CompleteAwareness(
            lookup,
            page.through_seq,
            tuple(selected),
            tuple(current_obligations),
            historical_omitted,
            instruction,
            content_instruction,
            provenance=(
                instruction.source,
                content_instruction.source,
                ResourceProvenance(str(path), digest, "captured typed optional awareness rows"),
                *(
                    WireProvenance(
                        MessageReference(row.assignment.wire_seq, row.assignment.message_id)
                    )
                    for row in selected
                ),
            ),
        )
        result.require_resource_budget(self.max_text_bytes)
        return result

    def _verify_live_inclusion(self, path: os.PathLike[str], lookup: str, owner: Thread) -> None:
        _require_no_private_owner_rename(self.index.bus.log.path.parent)
        registry = Registration(self.index.bus._registry.store.path)
        with _store_lock(registry.store.path, blocking=False):
            snapshot = registry.store._read_unlocked().snapshot()
            RegistryOwner(
                thread=owner,
                admission_generation=self.expected_admission_generation,
            ).require_snapshot(snapshot, "current registry owner turn changed")
        # A new read transaction, not the earlier candidate/decision snapshot,
        # observes a supported SQL generation advance during the optional read.
        with CoordinationStore.observing(Path(path), lock_timeout=0) as db:
            rows = _ParticipantOwner.read(
                db.execute(
                    "SELECT g.generation,p.committed,g.owner_thread FROM owner_generations g "
                    "JOIN participants p ON p.participant_lookup=g.owner_lookup "
                    "WHERE g.owner_lookup=?",
                    (lookup,),
                )
            )
            if rows != [_ParticipantOwner(self.expected_participant_generation, True, owner.name)]:
                raise ProjectionUnavailableError("current participant generation changed")
        _require_no_private_owner_rename(self.index.bus.log.path.parent)

    def _verify_schema_and_owner(
        self,
        db: sqlite3.Connection,
        initial: CommittedDelivery,
        assignment: WakeAssignment,
        owner: Thread,
    ) -> None:
        versions = SQLiteUserVersion.read(db.execute("PRAGMA user_version"))
        meta = _CoordinatorVersions.read(
            db.execute("SELECT schema_version,snapshot_version FROM schema_meta WHERE singleton=1")
        )
        if versions != [SQLiteUserVersion(COORDINATION_SCHEMA_VERSION)] or meta != [
            _CoordinatorVersions(COORDINATION_SCHEMA_VERSION, COORDINATION_SNAPSHOT_VERSION)
        ]:
            raise ProjectionUnavailableError("coordinator schema changed")
        assert_cohort_schema(db)
        assert_optional_awareness_schema(db)
        cohort_versions = _CohortVersion.read(
            db.execute("SELECT version FROM cohort_schema_meta WHERE singleton=1")
        )
        if cohort_versions != [_CohortVersion(COHORT_SCHEMA_VERSION)]:
            raise ProjectionUnavailableError("cohort schema changed")
        people = _ParticipantOwner.read(
            db.execute(
                "SELECT p.committed,g.owner_thread,g.generation FROM participants p "
                "JOIN owner_generations g ON g.owner_lookup=p.participant_lookup "
                "WHERE p.participant_lookup=?",
                (assignment.recipient_lookup,),
            )
        )
        if people != [_ParticipantOwner(self.expected_participant_generation, True, owner.name)]:
            raise ProjectionUnavailableError("selected participant owner changed")
        receipt = _receipt_matches(db, initial)
        if not any(current == assignment for current in receipt.assignments):
            raise ProjectionUnavailableError("current selected claim lost its sealed receipt")

    def _selected_decisions(
        self, db: sqlite3.Connection, root_id: str, lookup: str, through_seq: int
    ) -> list[_SelectedDecision]:
        assignments = WakeAssignment.select(
            db,
            where="recipient_lookup=? AND wire_seq>? AND wire_seq<=? ORDER BY wire_seq LIMIT ?",
            parameters=(lookup, self.after_seq, through_seq, self.max_rows + 1),
        )
        if len(assignments) > self.max_rows:
            raise ProjectionUnavailableError("binding decisions exceed the row budget")
        if not assignments:
            return []
        # Each bounded query decodes the canonical record at the SQLite boundary.
        # Assignments anchor completeness: a missing joined proof raises KeyError,
        # omitting the whole supplement rather than silently dropping its source.
        sequences = tuple(row.wire_seq for row in assignments)
        claims = tuple(row.assignment_id for row in assignments)
        marks = ",".join("?" for _ in assignments)
        receipts = {
            row.wire_seq: row
            for row in ClaimBatchReceipts.select(
                db,
                where=f"wire_root_id=? AND wire_seq IN ({marks})",
                parameters=(root_id, *sequences),
            )
        }
        members = {
            row.claim_id: row
            for row in ClaimBatchMembers.select(
                db,
                where=f"claim_id IN ({marks})",
                parameters=claims,
            )
        }
        deliveries = {
            row.wire_seq: row
            for row in CohortDeliveryReceipts.select(
                db,
                where=f"wire_root_id=? AND recipient_lookup=? AND wire_seq IN ({marks})",
                parameters=(root_id, lookup, *sequences),
            )
        }
        generations = {
            row.claim_id: row
            for row in AwarenessClaimGenerations.select(
                db,
                where=f"claim_id IN ({marks})",
                parameters=claims,
            )
        }
        return [
            _SelectedDecision.capture(
                row,
                receipts[row.wire_seq],
                members[row.assignment_id],
                deliveries[row.wire_seq],
                generations[row.assignment_id],
            )
            for row in assignments
        ]

    def _open_obligations(self, db: sqlite3.Connection, lookup: str) -> list[_OpenObligation]:
        obligations = ResponseObligation.select(
            db,
            where="execution_id IN (SELECT execution_id FROM executions "
            "WHERE owner_lookup=?) AND state IN ('pending','publishing','deferred') "
            "ORDER BY created_at_ms,execution_id LIMIT ?",
            parameters=(lookup, self.max_rows + 1),
        )
        if len(obligations) > self.max_rows:
            raise ProjectionUnavailableError("open obligations exceed the row budget")
        if not obligations:
            return []
        executions = tuple(dict.fromkeys(row.execution_id for row in obligations))
        marks = ",".join("?" for _ in executions)
        owners = {
            row.execution_id: row
            for row in ExecutionRecord.select(
                db,
                where=f"execution_id IN ({marks})",
                parameters=executions,
            )
        }
        links = ExecutionAssignmentLink.select(
            db,
            where=f"execution_id IN ({marks}) ORDER BY execution_id,ordinal LIMIT ?",
            parameters=(*executions, self.max_rows + 1),
        )
        if len(links) > self.max_rows:
            raise ProjectionUnavailableError("open obligation sources exceed the row budget")
        claims = tuple(row.assignment_id for row in links)
        if not claims:
            raise ProjectionUnavailableError("open obligation has no original sources")
        assignments = {
            row.assignment_id: row for row in WakeAssignment.select(
                db, where=f"assignment_id IN ({','.join('?' for _ in claims)})", parameters=claims,
            )
        }
        generations = {
            row.claim_id: row
            for row in AwarenessClaimGenerations.select(
                db,
                where=f"claim_id IN ({','.join('?' for _ in claims)})",
                parameters=claims,
            )
        }
        return [
            _OpenObligation.capture(
                row,
                owners[row.execution_id],
                link,
                generations[link.assignment_id],
                assignments[link.assignment_id],
            )
            for row in obligations
            for link in links
            if link.execution_id == row.execution_id
            and assignments[link.assignment_id].lifecycle.exact_target == row.exact_target
        ]
