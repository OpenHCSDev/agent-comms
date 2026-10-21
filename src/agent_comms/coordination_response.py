"""Owner-fenced response publication across the coordinator and bus.

Tx1 freezes an intent without a bus append. For the bounded append/Tx2, acquire
the shared wire lock, bus lock, registry lock, then the coordinator's exclusive
write lock. The registry lock is retained through the fsynced append and SQL terminal
commit; an explicitly witnessed live owner can be checked on that revision.
A stop before the boundary refuses publication, whereas a concurrent stop
linearizes after commit. The
rollback-journal transaction only spans one bounded, fsynced local bus row;
ordinary bus reads are completed *before* acquiring a coordinator transaction.
A crash may still leave a frozen PUBLISHING intent plus a durable bus row. The
read-only resolver can settle an EXISTING exact acknowledgement using the retained
current owner fence, but never creates an absent row or manufactures owner loss.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Type

from agent_comms.cohort_schema import assert_cohort_schema
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    PublicationUncertain,
    StaleFence,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.publications import (
    PublicationEvidence,
    PublicationIntents,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.response_acknowledgement import canonical_publication_key
from agent_comms.coordinator import Coordination
from agent_comms.message_bus import MessageBus
from agent_comms.messages import ConversationRole, InfoKind, Message, MessageKind, MessageRole
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.obligation_states import PublishedResponse, PublishingResponse
from agent_comms.owner_fence import OwnerFence
from agent_comms.private_runtime_schema import PrivateRuntimeSchema
from agent_comms.private_send_rules import RunningTurnRegistryCheck
from agent_comms.publisher import ResponseWrites
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.typed_table import (
    Column,
    DurableTables,
    ForeignKey,
    SharedFileTables,
    SQLiteForeignKeys,
    SqlValue,
    TypedRow,
    TypedTable,
)

from .locked_store import RegistryFile


class ResponseTable(SharedFileTables, DurableTables, PrivateRuntimeSchema):
    """An immutable response definition with its write checks and append evidence."""

    @classmethod
    def install(cls, store) -> None:
        install_private_response_schema(store)

    @classmethod
    def trigger_bodies(cls) -> dict[str, str]:
        return {
            f"{cls.declared_name}_{operation.lower()}_guard": " "
            f"BEFORE {operation} ON {cls.declared_name} "
            "BEGIN SELECT RAISE(ABORT,'response evidence is frozen'); END"
            for operation in ("UPDATE", "DELETE")
        }


@dataclass(frozen=True)
class PublicationAppendDispatches(ResponseTable, TypedTable):
    execution_id: str = field(
        metadata={"sql": Column(primary_key=True)}
    )
    exact_target: str = field(metadata={"sql": Column(primary_key=True, check="length(exact_target) BETWEEN 1 AND 256")})
    wire_root_id: str = field(
        metadata={
            "sql": Column(check="length(wire_root_id)=32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'")
        }
    )
    owner_generation: int = field(metadata={"sql": Column(check="owner_generation>0")})
    attempt_ordinal: int = field(metadata={"sql": Column(check="attempt_ordinal>0")})
    dispatched_at_ms: int = field(metadata={"sql": Column(check="dispatched_at_ms>=0")})
    without_rowid = True

    @classmethod
    def references(cls):
        return (ForeignKey(("execution_id", "exact_target"), PublicationIntents,
                          ("execution_id", "exact_target")),)

    def matches(self, wire_root_id: str, fence: OwnerFence, exact_target: str) -> bool:
        return self.matches_attempt(
            wire_root_id, fence.execution_id, fence.owner_generation,
            fence.attempt_ordinal, exact_target,
        )

    def matches_attempt(
        self, wire_root_id: str, execution_id: str, owner_generation: int,
        attempt_ordinal: int, exact_target: str,
    ) -> bool:
        return self == PublicationAppendDispatches(
            execution_id=execution_id,
            exact_target=exact_target,
            wire_root_id=wire_root_id,
            owner_generation=owner_generation,
            attempt_ordinal=attempt_ordinal,
            dispatched_at_ms=self.dispatched_at_ms,
        )

    @classmethod
    def resolve_retained(
        cls, store: Coordination, bus: MessageBus, boundary, *,
        owner_lookup: str, owner_thread: str, execution_id: str | None = None,
    ) -> tuple[RecoverySnapshot, ...]:
        """Finish only frozen responses whose exact keyed bus rows already exist.

        A live owner and a verified released owner share this one response-row
        resolver; neither path appends a second message or clears a pointer.
        """
        _require_bound_stores(bus, store)
        completed: list[RecoverySnapshot] = []
        wire_root_id = bus.log.private_marker_unlocked().root_id
        with store.session.read():
            execution_ids = tuple(SqlValue.for_annotation(str).read(
                store.session._connection.execute(
                    "SELECT DISTINCT i.execution_id FROM publication_intents i "
                    "JOIN executions e ON e.execution_id=i.execution_id "
                    "WHERE e.owner_lookup=? AND NOT EXISTS "
                    "(SELECT 1 FROM publication_receipts r "
                    "WHERE r.execution_id=i.execution_id "
                    "AND r.exact_target=i.exact_target) ORDER BY i.execution_id",
                    (owner_lookup,),
                )
            ))
        for current_id in execution_ids:
            if execution_id is not None and current_id != execution_id:
                continue
            snapshot = store.snapshots.get(current_id)
            if snapshot.execution.owner_thread != owner_thread:
                raise StaleFence("retained response belongs to another owner")
            for frozen in snapshot.publication_intents:
                if ResponseObligation.response_record(
                    snapshot.publication_evidence, frozen.exact_target
                ) is not None:
                    continue
                with store.session.writing() as db:
                    snapshot = store.snapshots.get(current_id)
                    _require_cohort_assignments(store, bus, snapshot, wire_root_id)
                    intent, obligation = snapshot.require_publishing_intent(
                        frozen.exact_target
                    )
                    attempt = snapshot.require_current_attempt()
                    dispatch = cls.one(
                        db, execution_id=current_id, exact_target=frozen.exact_target
                    )
                    if dispatch is None or not dispatch.matches_attempt(
                        wire_root_id, current_id, attempt.owner_generation,
                        attempt.attempt_ordinal, frozen.exact_target,
                    ):
                        raise PublicationUncertain("no matching durable append dispatch")
                    matched, _, _ = bus.log.keyed_publication_unlocked(intent)
                    if matched is None:
                        raise PublicationUncertain("no keyed bus acknowledgement; no resend authorized")
                    completed.append(
                        dispatch.complete(store, boundary, snapshot, intent, obligation, matched)
                    )
        return tuple(completed)

    def complete(
        self, store: Coordination, boundary, snapshot: RecoverySnapshot,
        intent: PublicationIntents, obligation: ResponseObligation, matched: Message,
    ) -> RecoverySnapshot:
        """Record one exact durable keyed row and finish its original attempt."""
        attempt = snapshot.require_current_attempt()
        if not self.matches_attempt(
            self.wire_root_id, snapshot.execution.execution_id,
            attempt.owner_generation, attempt.attempt_ordinal, intent.exact_target,
        ):
            raise IdentityConflict("response dispatch belongs to another root or owner")
        now = store.session.now(max(snapshot.execution.updated_at_ms, attempt.updated_at_ms))
        PublicationEvidence(
            execution_id=intent.execution_id,
            exact_target=intent.exact_target,
            seq=matched.seq,
            message_id=matched.message_id,
            received_at_ms=now,
        ).insert(store.session._connection)
        ResponseObligation.update(
            store.session._connection,
            where="execution_id=? AND exact_target=? AND revision=?",
            parameters=(intent.execution_id, intent.exact_target, obligation.revision),
            lifecycle=PublishedResponse(matched.message_id, matched.seq),
            revision=obligation.revision + 1,
            updated_at_ms=store.session.now(obligation.updated_at_ms),
        )
        store.snapshots.get(intent.execution_id).finish_publications(store.session, boundary)
        return store.snapshots.get(intent.execution_id)


@dataclass(frozen=True)
class SelectedResponseRoute(TypedRow):
    message_id: str
    exact_target: str
    envelope_digest: str
    audience_digest: str
    decisions_digest: str
    resolver_version: str
    policy_version: str
    recipient_lookup: str
    canonical_thread: str

    def require_source(self, initial, assignment: WakeAssignment) -> None:
        expected = SelectedResponseRoute(
            initial.message.message_id,
            initial.message.target,
            initial.audience.wire_envelope_digest,
            initial.audience.digest,
            initial.decisions_digest,
            assignment.resolver_version,
            assignment.policy_version,
            assignment.recipient_lookup,
            assignment.recipient,
        )
        if self != expected:
            raise IdentityConflict("response sealed route differs from committed source")


def _assert_response_schema(db: sqlite3.Connection) -> None:
    if not ResponseTable.stored_schema(db):
        raise PublicationActivationBlocked("private response schema is not installed")
    if ResponseTable.schema_differs(db) or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise PublicationActivationBlocked("private response schema has drifted")


def install_private_response_schema(store: Coordination) -> None:
    """Install the current response tables on a fresh coordinator only."""
    with store.session.writing() as db:
        if not ResponseTable.stored_schema(db):
            ResponseTable.create_schema(db)
        _assert_response_schema(db)


@dataclass(frozen=True, kw_only=True)
class LiveResponseOwner(RegistryOwner):
    """One captured registry owner; response finality does not depend on its goal."""

    check_type = RunningTurnRegistryCheck

    @property
    def recipient_lookup(self) -> str:
        return self.thread.incarnation.lookup

    def require_live(self, snapshot: RegistrySnapshot, fence: OwnerFence) -> None:
        # The registry resolves a renamed owner through its alias; the reply's
        # sender is the current canonical name, which must still be the fenced one.
        actual = self._snapshot_owner(
            snapshot, "response owner turn stopped or changed before publication", self.check_type,
        )
        if actual.name != fence.owner_thread:
            raise StaleFence("response turn belongs to a different fenced owner")
        self.require_active_turn()

    async def publish_responses(
        self, store_path: Path, bus: MessageBus[ResponseWrites], fence: OwnerFence,
        replies: tuple[Message, ...],
    ) -> RecoverySnapshot:
        """Wait for every route write before releasing the turn's resources.

        The worker owns its connection and each original ordered write fence.
        Only frozen proposals and the original owner/fence cross into it; no
        selected participant, caller connection or native work is transferred.
        """
        return await Coordination.run_async(
            store_path, partial(self._publish_owned, bus, fence, replies)
        )

    def resolve_retained(
        self, store: Coordination, bus: MessageBus
    ) -> tuple[RecoverySnapshot, ...]:
        """Complete only exact retained replies before this owner starts new work."""
        with store.session.boundary(bus) as boundary:
            self.require_snapshot(
                boundary.registry, "response owner changed before retained resolution"
            )
            return PublicationAppendDispatches.resolve_retained(
                store, bus, boundary, owner_lookup=self.recipient_lookup,
                owner_thread=self.thread.name,
            )

    def _publish_owned(
        self, bus: MessageBus[ResponseWrites], fence: OwnerFence,
        replies: tuple[Message, ...], store: Coordination,
    ) -> RecoverySnapshot:
        # SelectedSourceBatch.response_message already requires a nonempty
        # original route. Preparation freezes all of them before the first
        # append, preserving the original multi-route algorithm.
        for reply in replies:
            prepare_fenced_response(
                store, bus, fence, reply.body,
                exact_target=reply.target, owner_witness=self,
            )
        published: RecoverySnapshot
        for reply in replies:
            published = publish_fenced_response(
                store, bus, fence, exact_target=reply.target, owner_witness=self,
            ).value
            published.require_published_evidence(reply.target)
        # The last canonical snapshot contains every original assignment.
        # Complete required dependency settlement before the acquired worker
        # can close or report cancellation; no input/goal is reconstructed.
        from .comms import Comms

        goals = Comms(bus.log.path.parent).goals
        for assignment in published.assignments:
            goals.consume_reply_wait(self, assignment.source)
        return published


def _require_bound_stores(bus: MessageBus, store: Coordination) -> None:
    if (
        store.session.path.name != store.session.filename
        or Path(bus.log.path.parent).absolute() != Path(store.session.path.parent).absolute()
        or bus._registry.store.path.absolute() != (RegistryFile.path_in(bus.log.path.parent)).absolute()
    ):
        raise IdentityConflict("response bus and coordinator have different trusted roots")


def _require_cohort_assignments(
    store: Coordination,
    bus: MessageBus,
    snapshot: RecoverySnapshot,
    wire_root_id: str,
    *,
    terminal: bool = False,
) -> None:
    """Bind selected SQL facts to the ORIGINAL bus origin and exact reply route.

    A DM reply targets the original sender, not the DM's incoming target.
    Caller already holds the bus file lock before the SQL transaction.
    """
    db = store.session._connection
    _assert_response_schema(db)
    assert_cohort_schema(db)
    snapshot.require_wire_responses()
    metadata = bus.log.private_marker_unlocked()
    if metadata.root_id != wire_root_id:
        raise IdentityConflict("cohort bus root changed")
    originals = {initial.message.seq: initial for initial in bus.log.delivery_cohorts_unlocked(
        wire_root_id, tuple(assignment.wire_seq for assignment in snapshot.assignments))}
    for assignment in snapshot.assignments:
        initial = originals.get(assignment.wire_seq)
        routes_read = SelectedResponseRoute.read(
            db.execute(
                "SELECT r.message_id,r.exact_target,r.envelope_digest,r.audience_digest,"
                "r.decisions_digest,r.resolver_version,r.policy_version,m.recipient_lookup,"
                "d.canonical_thread FROM claim_batch_members m JOIN claim_batch_receipts r "
                "ON r.wire_root_id=m.wire_root_id AND r.wire_seq=m.wire_seq "
                "JOIN cohort_delivery_receipts d "
                "ON d.wire_root_id=m.wire_root_id AND d.wire_seq=m.wire_seq "
                "AND d.claim_id=m.claim_id AND d.kind='selected' "
                "WHERE m.claim_id=? AND m.wire_root_id=? AND r.sealed=1",
                (assignment.assignment_id, wire_root_id),
            )
        )
        if initial is None or len(routes_read) != 1:
            raise IdentityConflict("response claim lacks original bus/cohort authority")
        selected = {
            recipient.recipient_lookup
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            )
            for selected in decision.claim_decisions()
        }
        if initial.wire_root_id != wire_root_id or assignment.source != initial.message.reference:
            raise IdentityConflict("response claim conflicts with original bus source")
        routes_read[0].require_source(initial, assignment)
        if assignment.recipient_lookup not in selected:
            raise IdentityConflict("response recipient was not selected by the original source")
        engagement = (
            assignment.lifecycle.require_completion()
            if terminal
            else assignment.lifecycle.require_engagement()
        )
        if engagement.exact_target != initial.message.exact_reply_target:
            raise IdentityConflict("response claim conflicts with original selected bus route")


def _require_final_owner(
    store: Coordination,
    bus: MessageBus,
    fence: OwnerFence,
    wire_root_id: str,
    owner_witness: LiveResponseOwner,
    exact_target: str,
) -> RecoverySnapshot:
    snapshot, attempt = store.attempts.require_fence(fence)
    snapshot.require_final_response(owner_witness.recipient_lookup, exact_target)
    _require_cohort_assignments(store, bus, snapshot, wire_root_id)
    return snapshot


def _answered_by(bus, metadata, owner: LiveResponseOwner, registry, snapshot, obligation):
    """Selected originals and started following inputs share one published response.

    The caller holds the bus lock, so the source is opened under that lock.
    """
    from .input_disposition import InputDispositions
    from .private_bus_checkpoint import opened_private_checkpoint_unlocked
    from .response_conversation import ResponseConversation

    lease = owner.thread.require_turn_lease()
    inputs = InputDispositions(bus.log.path.parent).in_turn(lease.turn_id)
    with opened_private_checkpoint_unlocked(bus.log, metadata) as source:
        following = inputs.answered_by(lease, source, registry, obligation.exact_target)
    return ResponseConversation.answer_references(snapshot, obligation, following)


def prepare_fenced_response(
    store: Coordination,
    bus: MessageBus[ResponseWrites],
    fence: OwnerFence,
    payload: str,
    *,
    exact_target: str,
    message_type: Type[MessageKind] = InfoKind,
    role: MessageRole = ConversationRole(),
    timestamp: float | None = None,
    owner_witness: LiveResponseOwner,
) -> Applied[PublicationIntents] | AlreadyApplied[PublicationIntents]:
    """Tx1: freeze the only legal reply envelope and publishing obligation.

    This is an explicitly test-gated coordinator API, not Pi context proof or an
    authorization to infer backend completion from text. Only the current owner
    fence plus final backend/death evidence can prepare it.
    """
    _require_bound_stores(bus, store)
    if not payload:
        raise ValueError("response payload must be nonempty")
    with store.session.boundary(bus) as boundary:
        registry_snapshot = boundary.registry
        owner_witness.require_live(registry_snapshot, fence)
        metadata = bus.log.private_marker_unlocked()
        # The acquired canonical bus barrier verifies its sealed prefix, with
        # complete recovery/refusal on source or sidecar revision changes.
        with store.session.writing() as db:
            snapshot = _require_final_owner(store, bus, fence, metadata.root_id, owner_witness, exact_target)
            execution = snapshot.execution
            obligation = snapshot.require_wire_response(exact_target)
            target = obligation.exact_target
            existing = ResponseObligation.response_record(snapshot.publication_intents, target)
            answers = _answered_by(
                bus, metadata, owner_witness, registry_snapshot, snapshot, obligation
            )
            if existing is not None:
                snapshot.require_existing_preparation(
                    Message(
                        execution.owner_thread,
                        target,
                        payload,
                        message_type,
                        timestamp=existing.message.timestamp if timestamp is None else timestamp,
                        role=role,
                        answers=answers,
                    )
                )
                return AlreadyApplied(existing)
            obligation = snapshot.require_preparation(target)
            candidate = Message(
                execution.owner_thread,
                target,
                payload,
                message_type,
                timestamp=time.time() if timestamp is None else timestamp,
                role=role,
                answers=answers,
            )
            intent = PublicationIntents(
                execution_id=execution.execution_id,
                exact_target=target,
                message=candidate,
                publication_key=canonical_publication_key(
                    execution.execution_id, target
                ),
                expected_message_id=candidate.message_id,
            )
            intent.insert(db)
            ResponseObligation.update(
                db,
                where="execution_id=? AND exact_target=? AND state='pending' AND revision=?",
                parameters=(execution.execution_id, target, obligation.revision),
                lifecycle=PublishingResponse(),
                revision=obligation.revision + 1,
                updated_at_ms=store.session.now(obligation.updated_at_ms),
            )
            return Applied(intent)


def _terminal_replay(
    store: Coordination,
    fence: OwnerFence,
    bus: MessageBus,
    wire_root_id: str,
    owner_witness: LiveResponseOwner,
    exact_target: str,
) -> AlreadyApplied[RecoverySnapshot] | None:
    """A finished immutable acknowledgement may be re-read, never re-published."""
    snapshot = store.snapshots.get(fence.execution_id)
    if ResponseObligation.response_record(snapshot.publication_evidence, exact_target) is None:
        return None
    if snapshot.execution.owner_lookup != owner_witness.recipient_lookup:
        raise StaleFence("finished response belongs to a different SQL recipient")
    attempt = snapshot.require_attempt()
    if attempt.authority != fence.authority:
        raise StaleFence("finished response is not this owner's original attempt")
    intent, acknowledgement = snapshot.require_published_evidence(exact_target)
    _require_cohort_assignments(store, bus, snapshot, wire_root_id, terminal=snapshot.completed_response())
    matched, _, _ = bus.log.keyed_publication_unlocked(intent)
    if matched is None or matched.reference != acknowledgement.reference:
        raise PublicationUncertain("published SQL acknowledgement has no exact durable bus row")
    return AlreadyApplied(snapshot)


def _settle_fenced_response(
    store: Coordination,
    bus: MessageBus[ResponseWrites],
    fence: OwnerFence,
    *,
    allow_append: bool,
    exact_target: str,
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    _require_bound_stores(bus, store)
    # One registry revision is held through dispatch, append and Tx2. SQL's
    # current owner generation remains guarded by the transaction fence.
    with store.session.boundary(bus) as boundary:
        registry_snapshot = boundary.registry
        owner_witness.require_live(registry_snapshot, fence)
        metadata = bus.log.private_marker_unlocked()
        wire_root_id = metadata.root_id
        first_dispatch = False
        if allow_append:
            # A durable dispatch barrier BEFORE the external append distinguishes
            # a first attempt from a lost response/crash. If it commits but the
            # process dies before appending, no automatic retry is authorized.
            with store.session.writing() as db:
                _assert_response_schema(db)
                terminal = _terminal_replay(store, fence, bus, wire_root_id, owner_witness, exact_target)
                if terminal is not None:
                    return terminal
                snapshot = _require_final_owner(store, bus, fence, wire_root_id, owner_witness, exact_target)
                snapshot.require_publishing_intent(exact_target)
                prior = PublicationAppendDispatches.one(db, execution_id=fence.execution_id, exact_target=exact_target)
                if prior is None:
                    PublicationAppendDispatches(
                        fence.execution_id,
                        exact_target,
                        wire_root_id,
                        fence.owner_generation,
                        fence.attempt_ordinal,
                        store.session.now(snapshot.execution.updated_at_ms),
                    ).insert(db)
                    first_dispatch = True
                elif not prior.matches(wire_root_id, fence, exact_target):
                    raise IdentityConflict("response dispatch belongs to another root or owner")
        with store.session.writing() as db:
            _assert_response_schema(db)
            terminal = _terminal_replay(store, fence, bus, wire_root_id, owner_witness, exact_target)
            if terminal is not None:
                return terminal
            snapshot = _require_final_owner(store, bus, fence, wire_root_id, owner_witness, exact_target)
            intent, obligation = snapshot.require_publishing_intent(exact_target)
            dispatch = PublicationAppendDispatches.one(db, execution_id=fence.execution_id, exact_target=exact_target)
            if dispatch is None or not dispatch.matches(wire_root_id, fence, exact_target):
                raise PublicationUncertain("no matching durable append dispatch")
            matched, _, _ = bus.log.keyed_publication_unlocked(intent)
            if matched is None:
                if not first_dispatch:
                    raise PublicationUncertain("no keyed bus acknowledgement; no resend authorized")
                # This call created the durable dispatch barrier, and owns the
                # only authorized first append while its final fence is locked.
                from .response_conversation import ResponseConversation

                conversation = ResponseConversation.capture(bus, snapshot, obligation)
                matched = bus.publisher._publish_keyed_response_unlocked(
                    intent, conversation=conversation, registry_snapshot=registry_snapshot, activity=boundary.activity
                )
            return Applied(
                dispatch.complete(store, boundary, snapshot, intent, obligation, matched)
            )


def publish_fenced_response(
    store: Coordination,
    bus: MessageBus[ResponseWrites],
    fence: OwnerFence,
    *,
    exact_target: str,
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Explicit one-time owner append + Tx2. Never call after an uncertain crash."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=True, exact_target=exact_target, owner_witness=owner_witness
    )


def resolve_existing_response(
    store: Coordination,
    bus: MessageBus[ResponseWrites],
    fence: OwnerFence,
    *,
    exact_target: str,
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Read-only bus resolution of Tx1; absence never appends or retries."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=False, exact_target=exact_target, owner_witness=owner_witness
    )
