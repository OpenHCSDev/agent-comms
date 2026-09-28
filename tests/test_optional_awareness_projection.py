"""Sealed optional awareness omits incomplete work without changing delivery."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms import coordination_cohort as cohort
from agent_comms.bus_publication import CommittedInitial, stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination import ExecutionOrigin, WakeClaim
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import MutationStore
from agent_comms.declarations import Thread
from agent_comms.operations import Comms
from agent_comms.optional_awareness_projection import OptionalAwarenessProjection
from agent_comms.wake_candidate_index import WakeCandidateIndex
from test_coordinated_runtime import _fake_model

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private cohort requires POSIX")


def _root(
    tmp_path: Path, *, recipients: int = 2
) -> tuple[Comms, MutationStore, WakeCandidateIndex, str]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.register(Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()))
    for number in range(recipients):
        comms.register(
            Thread(
                f"member{number:03}",
                frozenset({"team"}),
                str(tmp_path),
                pid=os.getpid(),
                model="openai-codex/gpt-6-sol",
            )
        )
    root_id = comms.initialize_private_initial_protocol()
    store = MutationStore(str(root / "coordination.sqlite3"))
    install_private_cohort_schema(store)
    return comms, store, WakeCandidateIndex(comms.bus), root_id


def _accepted(
    comms: Comms, store: MutationStore, root_id: str, target: str, body: str
) -> tuple[CommittedInitial, WakeClaim]:
    message = comms.send_initial_cohort("sender", target, body)
    initial = comms.bus.read_initial_cohort(root_id, message.seq)
    for recipient in initial.audience.recipients:
        store.register_participant(
            recipient.recipient_lookup,
            recipient.canonical_thread,
            recipient.canonical_thread,
            committed=True,
        )
    receipt = accept_initial_cohort(comms.bus, root_id, message.seq, store).value
    assert len(receipt.claims) == 1
    return initial, receipt.claims[0]


def _owner(comms: Comms, name: str) -> Thread:
    original, epoch = comms.registry.live_owner_with_admission(name)
    owner, _ = comms.registry.claim_live_turn_with_admission(
        original, f"selected-{name}", expected_generation=epoch
    )
    return owner


def _projection(
    index: WakeCandidateIndex,
    store: MutationStore,
    owner: Thread,
    after_seq: int,
    through_seq: int,
    *,
    max_rows: int = 100,
) -> OptionalAwarenessProjection:
    assert owner.active_turn is not None
    epoch = owner.active_turn.admission_generation
    assert epoch is not None
    return OptionalAwarenessProjection(
        index,
        after_seq,
        through_seq,
        expected_participant_generation=store.participant(
            stable_thread_lookup(owner.created_at)
        ).generation,
        expected_admission_epoch=epoch,
        max_rows=max_rows,
    )


def test_optional_generation_insert_fault_rolls_back_only_optional_rows(
    tmp_path: Path, monkeypatch
) -> None:
    comms, store, _index, root_id = _root(tmp_path)
    try:
        message = comms.send_initial_cohort("sender", "#team", "@member000 @member001 act")
        initial = comms.bus.read_initial_cohort(root_id, message.seq)
        for recipient in initial.audience.recipients:
            store.register_participant(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        actual_assert = cohort.assert_optional_awareness_schema

        def inject_second_insert_failure(db):
            actual_assert(db)
            db.execute(
                "CREATE TRIGGER zz_optional_fault BEFORE INSERT "
                "ON awareness_claim_generations "
                "WHEN NEW.canonical_thread='member001' "
                "BEGIN SELECT RAISE(ABORT,'injected optional fault'); END"
            )

        monkeypatch.setattr(
            cohort, "assert_optional_awareness_schema", inject_second_insert_failure
        )
        accepted = accept_initial_cohort(comms.bus, root_id, message.seq, store).value
        assert len(accepted.claims) == 2
        assert (
            store._connection.execute(
                "SELECT sealed FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, message.seq),
            ).fetchone()[0]
            == 1
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM awareness_claim_generations WHERE wire_seq=?",
                (message.seq,),
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='zz_optional_fault'"
            ).fetchone()
            is None
        )  # the SAVEPOINT removed the transient fault too
    finally:
        store.close()


@pytest.mark.parametrize("phase", ["before_optional", "after_optional", "after_seal"])
def test_acceptance_fault_before_commit_never_leaves_partial_provenance(
    tmp_path: Path, monkeypatch, phase: str
) -> None:
    comms, store, _index, root_id = _root(tmp_path)
    try:
        message = comms.send_initial_cohort("sender", "member000", "fresh")
        initial = comms.bus.read_initial_cohort(root_id, message.seq)
        for recipient in initial.audience.recipients:
            store.register_participant(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        real_record = cohort._record_optional_owner_generations

        if phase in {"before_optional", "after_optional"}:

            def crash_optional(db, claims, wire_root_id, wire_seq):
                if phase == "after_optional":
                    real_record(db, claims, wire_root_id, wire_seq)
                raise RuntimeError("simulated interruption before commit")

            monkeypatch.setattr(cohort, "_record_optional_owner_generations", crash_optional)
        else:

            def crash_after_seal(*_args, **_kwargs):
                raise RuntimeError("simulated interruption after seal before commit")

            monkeypatch.setattr(cohort, "_receipt_matches", crash_after_seal)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            accept_initial_cohort(comms.bus, root_id, message.seq, store)
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_seq=?", (message.seq,)
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM wake_claims WHERE wire_seq=?", (message.seq,)
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM awareness_claim_generations WHERE wire_seq=?",
                (message.seq,),
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["missing", "drift"])
async def test_legacy_or_corrupt_optional_schema_omits_but_original_is_delivered(
    monkeypatch, damage: str
) -> None:
    if Path("/var").is_symlink() or not Path("/var/tmp").is_dir():
        pytest.skip("private selected runtime requires a real /var/tmp")
    with tempfile.TemporaryDirectory(prefix="pr94-legacy-optional-", dir="/var/tmp") as temp:
        root = Path(temp)
        comms, store, _index, root_id = _root(root)
        try:
            initial, claim = _accepted(comms, store, root_id, "member000", "mandatory work")
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            if damage == "missing":
                store._connection.execute("DROP TABLE awareness_claim_generations")
            else:
                store._connection.execute("DROP TRIGGER awareness_generation_insert_guard")
                store._connection.execute(
                    "CREATE TRIGGER awareness_generation_insert_guard BEFORE INSERT "
                    "ON awareness_claim_generations BEGIN SELECT RAISE(ABORT,'drift'); END"
                )
        finally:
            store.close()
        monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
        runner, calls = _fake_model()
        monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
        outcome = await runtime.run_one_sealed_claim(
            comms.root,
            wire_root_id=root_id,
            owner_name="member000",
            native_package=root,
        )
        assert outcome is not None and outcome.response_message_id
        assert outcome.claim_id == claim.claim_id and len(calls) == 1
        assert initial.message.body in calls[0][1]
        assert "Selected source decisions through " not in calls[0][1]


def test_selected_decision_and_open_obligation_are_both_source_cited(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, claim = _accepted(comms, store, root_id, "member000", "work")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        store.create_execution(
            "reply000",
            ExecutionOrigin.WIRE,
            claim.recipient_lookup,
            owner.name,
            1,
            claim_ids=(claim.claim_id,),
            exact_target="sender",
        )
        current = store.claim(claim.claim_id)
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, current, owner)
        assert result.mandatory_complete and result.omission_reason is None
        context = json.loads(result.text)
        assert context["selected"] == [
            {
                "claim_id": claim.claim_id,
                "disposition": "engaged",
                "message_id": initial.message.message_id,
                "source_seq": initial.message.seq,
                "target": "member000",
                "wake_mode": "full",
            }
        ]
        assert context["open_obligations"] == [
            {"execution_id": "reply000", "state": "pending", "target": "sender"}
        ]
    finally:
        store.close()


def test_stale_index_unsealed_candidate_and_replaced_owner_omit(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, claim = _accepted(comms, store, root_id, "member000", "first")
        owner = _owner(comms, "member000")
        assert not _projection(index, store, owner, 0, 1)(initial, claim, owner).mandatory_complete
        index.maintain(rebuild=True)
        assert _projection(index, store, owner, 0, 1)(initial, claim, owner).mandatory_complete

        second = comms.send_initial_cohort("sender", "member000", "not yet sealed")
        index.maintain()
        # The page checkpoint can outrun the caller's minimum source watermark.
        # It must include the later candidate or omit the whole supplement.
        assert not _projection(index, store, owner, 0, initial.message.seq)(
            initial, claim, owner
        ).mandatory_complete
        unavailable = _projection(index, store, owner, 0, second.seq)(initial, claim, owner)
        assert not unavailable.mandatory_complete and unavailable.text == ""
        replaced = replace(owner, created_at=owner.created_at + 10)
        assert not _projection(index, store, owner, 0, 1)(
            initial, claim, replaced
        ).mandatory_complete
    finally:
        store.close()


def test_open_obligation_budget_cannot_be_hidden_by_selected_cursor(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        older, old_claim = _accepted(comms, store, root_id, "member000", "older")
        newer, new_claim = _accepted(comms, store, root_id, "member000", "newer")
        owner = _owner(comms, "member000")
        for number, claim in enumerate((old_claim, new_claim)):
            store.create_execution(
                f"reply{number}",
                ExecutionOrigin.WIRE,
                claim.recipient_lookup,
                owner.name,
                1,
                claim_ids=(claim.claim_id,),
                exact_target="sender",
            )
        index.maintain(rebuild=True)
        current = store.claim(new_claim.claim_id)
        # Only the newer selected row lies after the cursor, but both exact
        # response obligations are still open and must be represented.
        assert older.message.seq == 1
        result = _projection(index, store, owner, older.message.seq, newer.message.seq, max_rows=1)(
            newer, current, owner
        )
        assert not result.mandatory_complete and result.text == ""
    finally:
        store.close()


def test_captured_owner_sql_generation_advance_omits(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, claim = _accepted(comms, store, root_id, "member000", "original")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        builder = _projection(index, store, owner, 0, initial.message.seq)
        assert builder(initial, claim, owner).mandatory_complete
        store.advance_owner_generation(claim.recipient_lookup, owner.name, expected_generation=1)
        stale = builder(initial, claim, owner)
        assert not stale.mandatory_complete and stale.text == ""
    finally:
        store.close()


def test_new_snapshot_after_same_name_generation_bump_omits_old_claim(
    tmp_path: Path,
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, claim = _accepted(comms, store, root_id, "member000", "old pending")
        store.advance_owner_generation(claim.recipient_lookup, "member000", expected_generation=1)
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        # Even a newly captured SQL gen2+registry turn cannot assign a gen1
        # frozen claim to gen2: no per-claim generation was sealed at accept.
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, claim, owner)
        assert not result.mandatory_complete and result.text == ""
    finally:
        store.close()


def test_fresh_gen2_selected_and_old_pending_obligation_are_scoped(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        old, old_claim = _accepted(comms, store, root_id, "member000", "old pending")
        store.create_execution(
            "old-reply",
            ExecutionOrigin.WIRE,
            old_claim.recipient_lookup,
            "member000",
            1,
            claim_ids=(old_claim.claim_id,),
            exact_target="sender",
        )
        store.advance_owner_generation(
            old_claim.recipient_lookup, "member000", expected_generation=1
        )
        current, claim = _accepted(comms, store, root_id, "member000", "new pending")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, current.message.seq)(current, claim, owner)
        assert result.mandatory_complete and result.omitted_count == 2
        context = json.loads(result.text)
        assert [row["claim_id"] for row in context["selected"]] == [claim.claim_id]
        assert context["open_obligations"] == []
        assert old_claim.claim_id not in result.text and "old-reply" not in result.text
        assert old.message.seq < current.message.seq
    finally:
        store.close()


def test_fresh_gen2_only_selected_is_available(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        lookup = stable_thread_lookup(comms.registry.require("member000").created_at)
        store.register_participant(lookup, "member000", "member000", committed=True)
        store.advance_owner_generation(lookup, "member000", expected_generation=1)
        initial, claim = _accepted(comms, store, root_id, "member000", "fresh")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, claim, owner)
        assert result.mandatory_complete and result.omitted_count == 0
        assert [row["claim_id"] for row in json.loads(result.text)["selected"]] == [claim.claim_id]
    finally:
        store.close()


def test_owner_generation_advance_during_snapshot_omits_at_inclusion(
    tmp_path: Path, monkeypatch
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        initial, claim = _accepted(comms, store, root_id, "member000", "original")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        builder = _projection(index, store, owner, 0, initial.message.seq)
        original = OptionalAwarenessProjection._open_obligations

        def race(self, db, lookup, owner_name):
            with MutationStore(str(index.bus._path.with_name("coordination.sqlite3"))) as other:
                other.advance_owner_generation(lookup, owner.name, expected_generation=1)
            return original(self, db, lookup, owner_name)

        monkeypatch.setattr(OptionalAwarenessProjection, "_open_obligations", race)
        stale = builder(initial, claim, owner)
        assert not stale.mandatory_complete and stale.text == ""
    finally:
        store.close()


def test_older_receipt_message_id_mismatch_omits_entire_context(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        prior, _ = _accepted(comms, store, root_id, "member000", "prior")
        initial, claim = _accepted(comms, store, root_id, "member000", "current")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        builder = _projection(index, store, owner, 0, initial.message.seq)
        assert builder(initial, claim, owner).mandatory_complete
        # Disposable legacy/disk inconsistency only: supported writes freeze
        # receipt facts. Restore the original trigger before checking schema.
        db = store._connection
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='cohort_receipt_update_guard'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER cohort_receipt_update_guard")
        db.execute(
            "UPDATE claim_batch_receipts SET message_id=? WHERE wire_root_id=? AND wire_seq=?",
            ("forged-older-id", root_id, prior.message.seq),
        )
        db.execute(trigger)
        stale = builder(initial, claim, owner)
        assert not stale.mandatory_complete and stale.text == ""
    finally:
        store.close()


async def test_real_selected_caller_after_rename_injects_only_new_generation(
    monkeypatch,
) -> None:
    if Path("/var").is_symlink() or not Path("/var/tmp").is_dir():
        pytest.skip("private selected runtime requires a real /var/tmp")
    with tempfile.TemporaryDirectory(prefix="pr94-renamed-awareness-", dir="/var/tmp") as temp:
        root = Path(temp)
        comms, store, index, root_id = _root(root)
        try:
            _old, old_claim = _accepted(comms, store, root_id, "member000", "old pending")
            comms._rename_thread("member000", "gamma")
            current, claim = _accepted(comms, store, root_id, "gamma", "new original")
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            index.maintain(rebuild=True)
        finally:
            store.close()
        monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
        runner, calls = _fake_model()
        monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
        outcome = await runtime.run_one_sealed_claim(
            comms.root, wire_root_id=root_id, owner_name="gamma", native_package=root
        )
        assert outcome is not None and outcome.response_message_id
        assert outcome.claim_id == claim.claim_id and len(calls) == 1
        assert current.message.body in calls[0][1]
        assert "Selected source decisions through " in calls[0][1]
        assert claim.claim_id in calls[0][1]
        assert old_claim.claim_id not in calls[0][1]
        assert "Nonbinding rows omitted: 1" in calls[0][1]


def test_normal_rename_does_not_inject_old_selected_claim_into_new_owner(
    tmp_path: Path,
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        old, old_claim = _accepted(comms, store, root_id, "member000", "old pending")
        comms._rename_thread("member000", "gamma")
        current_message = comms.send_initial_cohort("sender", "gamma", "new selected")
        current = comms.bus.read_initial_cohort(root_id, current_message.seq)
        receipt = accept_initial_cohort(comms.bus, root_id, current_message.seq, store).value
        assert len(receipt.claims) == 1
        current_claim = receipt.claims[0]
        index.maintain(rebuild=True)
        owner = _owner(comms, "gamma")
        result = _projection(index, store, owner, 0, current.message.seq)(
            current, current_claim, owner
        )
        assert old_claim.recipient == "member000" and current_claim.recipient == "gamma"
        assert old.message.seq < current.message.seq
        assert result.mandatory_complete and result.omitted_count == 1
        selected = json.loads(result.text)["selected"]
        assert [row["claim_id"] for row in selected] == [current_claim.claim_id]
        assert old_claim.claim_id not in result.text
    finally:
        store.close()


def test_other_owner_decision_never_enters_selected_context(tmp_path: Path) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        _accepted(comms, store, root_id, "member001", "foreign-owner-message")
        initial, claim = _accepted(comms, store, root_id, "member000", "ours")
        index.maintain(rebuild=True)
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, claim, owner)
        assert result.mandatory_complete
        context = json.loads(result.text)
        assert [row["source_seq"] for row in context["selected"]] == [initial.message.seq]
        assert "foreign-owner-message" not in result.text
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
        initial, claim = _accepted(comms, store, root_id, "#team", "@member000 do this")
        index.maintain(rebuild=True, max_bytes=8 * 1024 * 1024)
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, claim, owner)
        assert result.mandatory_complete
        assert len(json.loads(result.text)["selected"]) == 1
    finally:
        store.close()


def test_101_prior_initials_for_other_recipient_do_not_require_a_bus_scan(
    tmp_path: Path,
) -> None:
    comms, store, index, root_id = _root(tmp_path)
    try:
        for number in range(100):
            comms.send_initial_cohort("sender", "member001", f"other owner {number}")
        initial, claim = _accepted(comms, store, root_id, "member000", "current work")
        assert initial.message.seq == 101
        assert index.maintain(rebuild=True, max_rows=256, max_bytes=8 * 1024 * 1024)
        owner = _owner(comms, "member000")
        result = _projection(index, store, owner, 0, initial.message.seq)(initial, claim, owner)
        assert result.mandatory_complete
        context = json.loads(result.text)
        assert [row["source_seq"] for row in context["selected"]] == [101]
    finally:
        store.close()
