"""Sealed optional awareness omits incomplete work without changing delivery."""

from __future__ import annotations



from agent_comms.repo import Checkout
from bus_record_fixture import prepare_native_comms
from agent_comms.field_codec import FieldCodec, MessageId, TurnId
from agent_comms.lifecycle_capability import Executing

from conftest import publish_initial, without_mutation_guards
from agent_comms.messages import NoticeRole
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.bus_publication import Delivered
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import AwarenessClaimGenerations, ClaimBatchAcknowledgements
from agent_comms.comms import Comms, wire
from conftest import accept_initial
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.assignment_states import EngagedAssignment
from agent_comms.wake_policy import FullWake
from agent_comms.coordination_tables.executions import WireOrigin
from agent_comms.coordinator import Coordination
from agent_comms.optional_awareness_projection import OptionalAwarenessProjection
from agent_comms.registration import Registration
from agent_comms.selected_source_batch import SelectedSource
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import (
    WakeCandidateIndex,
    ProjectionUnavailableError,
)
from agent_comms.thread_model import DeclaredModel

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private cohort requires POSIX")


def _root(
    tmp_path: Path, *, recipients: int = 2
) -> tuple[Comms, Coordination, WakeCandidateIndex, str]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(
        Thread('sender', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    for number in range(recipients):
        comms.registry.declare(
            Thread(f'member{number:03}', frozenset({'team'}), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model=DeclaredModel('openai-codex/gpt-6-sol'))
        )
    root_id = comms.registry.prepare_native_root().namespace
    store = Coordination(str(root / "coordination.sqlite3"))
    return comms, store, WakeCandidateIndex(comms.bus.bus), root_id


def _accepted(
    comms: Comms, store: Coordination, root_id: str, target: str, body: str
) -> tuple[Delivered, WakeAssignment]:
    message = publish_initial(comms, "sender", target, body)
    initial = comms.bus.bus.delivery(message.seq)
    for recipient in initial.recipients:
        store.participants.register(
            recipient.recipient_lookup,
            committed=True,
        )
    (assignment,) = accept_initial(comms.bus, root_id, message.seq, store)
    return initial, assignment


def _owner(comms: Comms, name: str) -> Thread:
    original, claimed_generation = comms.registry.live_owner_with_claim(name)
    owner, _ = comms.registry.lease_live_turn_with_claim(
        original, TurnId.mint(), expected_generation=claimed_generation
    )
    return owner


def _projection(
    index: WakeCandidateIndex,
    store: Coordination,
    owner: Thread,
    after_seq: int,
    through_seq: int,
    *,
    max_rows: int = 100,
) -> OptionalAwarenessProjection:
    active = owner.turn_record.read().require_active()
    active.require(Executing, ValueError("turn must execute"))
    claimed_generation = active.claimed_generation.value
    return OptionalAwarenessProjection(
        index,
        after_seq,
        through_seq,
        registry=Registration(index.bus.path.with_name("registry.json")),
        expected_participant_generation=store.participants.get(
            owner.incarnation.lookup
        ).owner_identity.generation,
        expected_claimed_generation=claimed_generation,
        max_rows=max_rows,
    )


def test_selected_decision_and_open_obligation_are_both_source_cited(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, assignment = _accepted(comms, store, root_id, "member000", "work")
        owner = _owner(comms, "member000")
        store.create(
            "reply000",
            WireOrigin,
            assignment.recipient_lookup,
            1,
            sources=(SelectedSource(assignment.assignment_id, store.assignments, initial),),
        )
        current = store.assignments.get(assignment.assignment_id)
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, current, owner)
        context = result.context()
        assert context["selected"] == [
            {
                "claim_id": assignment.assignment_id,
                "disposition": EngagedAssignment,
                "message_id": initial.message.message_id,
                "source_seq": initial.message.seq,
                "target": "member000",
                "wake_mode": FullWake,
            }
        ]
        assert context["open_obligations"] == [
            {"execution_id": "reply000", "state": "pending", "target": "sender"}
        ]
    finally:
        store.close()


def test_committed_candidates_and_replaced_owner(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, assignment = _accepted(comms, store, root_id, "member000", "first")
        owner = _owner(comms, "member000")
        assert _projection(index, store, owner, 0, 1)(initial, assignment, owner).context()["selected"]

        second = publish_initial(comms, "sender", "member000", "not yet sealed")
        # The committed prefix can outrun the caller's minimum source watermark.
        # It must include the later candidate or omit the whole supplement.
        assert 'Optional awareness omitted' in _projection(index, store, owner, 0, initial.message.seq)(initial, assignment, owner).render(32768)
        unavailable = _projection(index, store, owner, 0, second.seq)(initial, assignment, owner)
        assert 'Optional awareness omitted' in unavailable.render(32768)
        replaced = replace(owner, created_at=owner.created_at + 10)
        assert 'Optional awareness omitted' in _projection(index, store, owner, 0, 1)(initial, assignment, replaced).render(32768)
    finally:
        store.close()


def test_open_obligation_budget_cannot_be_hidden_by_selected_cursor(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        older, old_claim = _accepted(comms, store, root_id, "member000", "older")
        newer, new_claim = _accepted(comms, store, root_id, "member000", "newer")
        owner = _owner(comms, "member000")
        for number, (assignment, delivery) in enumerate(((old_claim, older), (new_claim, newer))):
            store.create(
                f"reply{number}",
                WireOrigin,
                assignment.recipient_lookup,
                1,
                sources=(SelectedSource(assignment.assignment_id, store.assignments, delivery),),
            )
        current = store.assignments.get(new_claim.assignment_id)
        # Only the newer selected row lies after the cursor, but both exact
        # response obligations are still open and must be represented.
        assert older.message.seq == 1
        result = _projection(index, store, owner, older.message.seq, newer.message.seq, max_rows=1)(
            newer, current, owner
        )
        assert 'Optional awareness omitted' in result.render(32768)
    finally:
        store.close()


def test_captured_owner_sql_generation_advance_omits(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, assignment = _accepted(comms, store, root_id, "member000", "original")
        owner = _owner(comms, "member000")
        builder = _projection(index, store, owner, 0, initial.message.seq)
        assert builder(initial, assignment, owner).context()["selected"]
        store.participants.advance_generation(
            assignment.recipient_lookup, expected_generation=1
        )
        stale = builder(initial, assignment, owner)
        assert 'Optional awareness omitted' in stale.render(32768)
    finally:
        store.close()


def test_new_snapshot_after_same_name_generation_bump_omits_old_claim(
    tmp_path: Path,
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, assignment = _accepted(comms, store, root_id, "member000", "old pending")
        store.participants.advance_generation(
            assignment.recipient_lookup, expected_generation=1
        )
        owner = _owner(comms, "member000")
        # Even a newly captured SQL gen2+registry turn cannot assign a gen1
        # frozen claim to gen2: no per-claim generation was sealed at accept.
        result = _projection(index, store, owner, 0, initial.message.seq)(
            initial, assignment, owner
        )
        assert 'Optional awareness omitted' in result.render(32768)
    finally:
        store.close()


def test_fresh_gen2_selected_and_old_pending_obligation_are_scoped(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        old, old_claim = _accepted(comms, store, root_id, "member000", "old pending")
        store.create(
            "old-reply",
            WireOrigin,
            old_claim.recipient_lookup,
            1,
            sources=(SelectedSource(old_claim.assignment_id, store.assignments, old),),
        )
        store.participants.advance_generation(
            old_claim.recipient_lookup, expected_generation=1
        )
        current, assignment = _accepted(comms, store, root_id, "member000", "new pending")
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, current.message.seq)(
            current, assignment, owner
        )
        assert result.omitted_count == 2
        context = result.context()
        assert [row["claim_id"] for row in context["selected"]] == [assignment.assignment_id]
        assert context["open_obligations"] == []
        assert old_claim.assignment_id not in result.render(
            32768
        ) and "old-reply" not in result.render(32768)
        assert old.message.seq < current.message.seq
    finally:
        store.close()


def test_fresh_gen2_only_selected_is_available(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        lookup = comms.registry.require("member000").incarnation.lookup
        store.participants.register(lookup, committed=True)
        store.participants.advance_generation(lookup, expected_generation=1)
        initial, assignment = _accepted(comms, store, root_id, "member000", "fresh")
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(
            initial, assignment, owner
        )
        assert result.omitted_count == 0
        assert [row["claim_id"] for row in result.context()["selected"]] == [
            assignment.assignment_id
        ]
    finally:
        store.close()


def test_older_receipt_message_id_mismatch_omits_entire_context(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        prior, _ = _accepted(comms, store, root_id, "member000", "prior")
        initial, assignment = _accepted(comms, store, root_id, "member000", "current")
        owner = _owner(comms, "member000")
        builder = _projection(index, store, owner, 0, initial.message.seq)
        assert builder(initial, assignment, owner).context()["selected"]
        # Disposable disk inconsistency only: supported writes freeze
        # acknowledgement facts. Restore declared guards before checking schema.
        db = store.session._connection
        with without_mutation_guards(db, ClaimBatchAcknowledgements):
            db.execute(
                "UPDATE claim_batch_receipts SET message_id=? WHERE wire_root_id=? AND wire_seq=?",
                (FieldCodec.encode(MessageId.mint("forged-older-id")), root_id, prior.message.seq),
            )
        stale = builder(initial, assignment, owner)
        assert 'Optional awareness omitted' in stale.render(32768)
    finally:
        store.close()


def test_normal_rename_preserves_selected_claims_for_the_same_owner(
    tmp_path: Path,
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        old, old_claim = _accepted(comms, store, root_id, "member000", "old pending")
        comms.threads._rename_thread("member000", "gamma")
        current_message = publish_initial(comms, "sender", "gamma", "new selected")
        current = comms.bus.bus.delivery(current_message.seq)
        (current_claim,) = accept_initial(comms.bus, root_id, current_message.seq, store)
        owner = _owner(comms, "gamma")
        result = _projection(index, store, owner, 0, current.message.seq)(
            current, current_claim, owner
        )
        assert old_claim.recipient == "member000" and current_claim.recipient == "gamma"
        assert old.message.seq < current.message.seq
        assert result.omitted_count == 0
        selected = result.context()["selected"]
        assert [row["claim_id"] for row in selected] == [old_claim.assignment_id, current_claim.assignment_id]
        assert old_claim.assignment_id in result.render(32768)
        captured = AwarenessClaimGenerations.one(
            store.session._connection, claim_id=old_claim.assignment_id,
        )
        current_owner = store.participants.get(current_claim.recipient_lookup).owner_identity
        # A supported rename preserves generation, but it does not make unrelated
        # lookups or a capture from a future generation historical evidence.
        for invalid in (
            replace(captured, owner_lookup="unrelated-owner"),
            replace(captured, owner_generation=current_owner.generation + 1),
        ):
            with pytest.raises(ProjectionUnavailableError):
                invalid.current(current_owner)
    finally:
        store.close()


def test_other_owner_decision_never_enters_selected_context(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        _accepted(comms, store, root_id, "member001", "foreign-owner-message")
        initial, assignment = _accepted(comms, store, root_id, "member000", "ours")
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(
            initial, assignment, owner
        )
        context = result.context()
        assert [row["source_seq"] for row in context["selected"]] == [initial.message.seq]
        assert "foreign-owner-message" not in result.render(32768)
    finally:
        store.close()


@pytest.mark.parametrize("recipients", [10, 150])
def test_many_frozen_members_do_not_become_selected_authority(
    tmp_path: Path, recipients: int
) -> None:
    comms, store, index, root_id = _root(tmp_path, recipients=recipients)
    try:
        # The chosen owner has one selected source among the large frozen N;
        # no-wake members remain absent from its mandatory selected context.
        initial, assignment = _accepted(comms, store, root_id, "#team", "@member000 do this")
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(
            initial, assignment, owner
        )
        assert len(result.context()["selected"]) == 1
    finally:
        store.close()


def test_101_prior_initials_for_other_recipient_do_not_require_a_bus_scan(
    tmp_path: Path,
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        for number in range(100):
            publish_initial(comms, "sender", "member001", f"other owner {number}")
        initial, assignment = _accepted(comms, store, root_id, "member000", "current work")
        assert initial.message.seq == 101
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(
            initial, assignment, owner
        )
        context = result.context()
        assert [row["source_seq"] for row in context["selected"]] == [101]
    finally:
        store.close()


def test_saved_wire_awareness_preserves_passive_authority_and_rejects_incomplete_proof(
    tmp_path: Path,
) -> None:
    """Real frozen wire and SQLite lifecycle; projection cannot create permission."""
    import time

    from agent_comms.assignment_states import PassiveAssignment
    from agent_comms.coordination_tables.responses import ResponseObligation

    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, assignment = _accepted(comms, store, root_id, "#team", "@member000 act")
        notice = publish_initial(comms,
            "sender", "member000", "Passive retained context", role=NoticeRole()
        )
        passive = accept_initial(comms.bus, root_id, notice.seq, store)[0]
        assert isinstance(passive.lifecycle, PassiveAssignment)
        with pytest.raises(IdentityConflict):
            passive.lifecycle.require_engageable()
        observer = comms.registry.require("member001").incarnation.lookup
        assert not WakeAssignment.select(
            store.session._connection,
            where="recipient_lookup=? AND wire_seq=?",
            parameters=(observer, initial.message.seq),
        )
        owner = _owner(comms, "member000")
        store.create(
            "wire-awareness-reply",
            WireOrigin,
            assignment.recipient_lookup,
            1,
            sources=(SelectedSource(assignment.assignment_id, store.assignments, initial),),
        )
        assignment = store.assignments.get(assignment.assignment_id)
        builder = _projection(index, store, owner, 0, notice.seq)
        sequence_before = comms.bus.bus.revision()
        assignments_before = WakeAssignment.select(store.session._connection)
        obligations_before = ResponseObligation.select(store.session._connection)
        started = time.monotonic()
        result = builder(initial, assignment, owner)
        elapsed = time.monotonic() - started
        context = result.context()
        assert {row["claim_id"] for row in context["selected"]} == {
            assignment.assignment_id,
            passive.assignment_id,
        }
        passive_context = next(
            row for row in context["selected"] if row["claim_id"] == passive.assignment_id
        )
        assert passive_context["wake_mode"] is type(passive.lifecycle.mode)
        assert len(context["open_obligations"]) == 1
        assert context["open_obligations"][0]["execution_id"] == "wire-awareness-reply"
        assert WakeAssignment.select(store.session._connection) == assignments_before
        assert ResponseObligation.select(store.session._connection) == obligations_before
        assert comms.bus.bus.revision() == sequence_before
        print(f"actual complete wire projection: {elapsed:.6f}s", flush=True)

        # A reused numeric PID with a different recorded process birth is not
        # the selected owner, even when every old numeric-PID field agrees.
        identity = owner.process_identity
        assert identity is not None
        replaced_process = replace(
            owner, process_identity=replace(identity, start_time=identity.start_time + 1)
        )
        changed = builder(initial, assignment, replaced_process)
        assert 'Optional awareness omitted' in changed.render(32768)
        assert builder(initial, assignment, owner).context()["selected"]

        # Simulate damaged derived proof, restoring the real schema before read.
        # The passive row remains a completeness requirement, not an inner-join
        # casualty silently omitted while the original is called complete.
        db = store.session._connection
        with without_mutation_guards(db, AwarenessClaimGenerations):
            db.execute(
                "DELETE FROM awareness_claim_generations WHERE claim_id=?", (passive.assignment_id,)
            )
        missing = builder(initial, assignment, owner)
        assert 'Optional awareness omitted' in missing.render(32768)
        assert WakeAssignment.select(db) == assignments_before
        assert ResponseObligation.select(db) == obligations_before
        assert comms.bus.bus.revision() == sequence_before
    finally:
        store.close()


def test_full_bounded_awareness_window_keeps_foreground_deadline(tmp_path: Path) -> None:
    import time

    comms, store, index, root_id = _root(tmp_path, recipients=1)
    try:
        for number in range(100):
            initial, assignment = _accepted(comms, store, root_id, "member000", f"work {number}")
        owner = _owner(comms, "member000")
        newest = _projection(index, store, owner, initial.message.seq - 1, initial.message.seq)
        assert newest(initial, assignment, owner).context()["selected"]
        all_rows = _projection(index, store, owner, 0, initial.message.seq)
        started = time.monotonic()
        bounded = all_rows(initial, assignment, owner)
        elapsed = time.monotonic() - started
        # One hundred complete claim descriptions exceed the existing 16KiB
        # optional text budget; omission must still fit the actual 250ms gate.
        assert 'Optional awareness omitted' in bounded.render(32768)
        assert bounded.reason == "ProjectionUnavailableError"
        assert elapsed < 0.250
        print(f"actual 100-row optional projection: {elapsed:.6f}s", flush=True)
    finally:
        store.close()
