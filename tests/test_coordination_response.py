"""Actual fsynced bus + SQLite response boundaries, default OFF outside opted-in roots."""

from __future__ import annotations

import os
import selectors
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination import (
    AttemptPhase,
    ClaimDisposition,
    ExecutionOrigin,
    ExecutionStatus,
    ObligationState,
    WakeMode,
)
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import (
    install_private_response_schema,
    prepare_fenced_response,
    publish_fenced_response,
    resolve_existing_response,
)
from agent_comms.coordination_store import (
    AlreadyApplied,
    IdentityConflict,
    MutationStore,
    PublicationActivationBlocked,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
    prepare_fence_token,
)
from agent_comms.declarations import MessageBus, Thread, ThreadRegistry, _store_lock
from agent_comms.operations import Comms
from agent_comms.retained_turn_settlement import (
    RetainedTurnIdentity,
    RetainedTurnReceipt,
    admit_test_retained_turn,
    record_test_turn_settled,
)
from agent_comms.wake import derive_exact_reply_target

# Private bus publication requires POSIX owner/mode ancestry; Windows stat
# emulation cannot attest this boundary or exercise its durability contract.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus needs POSIX ownership")


@dataclass
class Fixture:
    comms: Comms
    bus: MessageBus
    store: MutationStore
    root_id: str
    origin_seq: int
    reply_target: str
    owner_lookup: str
    claim_id: str
    fence: object
    retained_identity: RetainedTurnIdentity | None = None

    def close(self) -> None:
        self.store.close()


def _ready(
    tmp_path: Path,
    *,
    direct: bool = False,
    process_dead: bool = True,
    retained: bool = False,
    finalize: bool = True,
) -> Fixture:
    comms = Comms(tmp_path / "wire", private_initial_writes=True)
    for name in ("sender", "owner"):
        comms.register(Thread(name, frozenset({"team"}), worktree=str(tmp_path)))
    root_id = comms.initialize_private_initial_protocol()
    target = "owner" if direct else "#team"
    original = comms.send_initial_cohort("sender", target, "Need owner to consider and reply.")
    original_record = comms.bus.read_initial_cohort(root_id, original.seq)
    assert len(original_record.audience.recipients) == 1
    recipient = original_record.audience.recipients[0]
    bus = MessageBus(comms.root / "bus.jsonl", comms.registry, private_response_writes=True)
    store = MutationStore(str(comms.root / "coordination.sqlite3"), clock_ms=lambda: 1_000)
    install_private_cohort_schema(store)
    install_private_response_schema(store)
    store.register_participant(
        recipient.recipient_lookup,
        recipient.canonical_thread,
        recipient.canonical_thread,
        committed=True,
    )
    accepted = accept_initial_cohort(comms.bus, root_id, original.seq, store)
    assert accepted.value.member_count == accepted.value.claim_count == 1
    claim = accepted.value.claims[0]
    reply_target = derive_exact_reply_target(original)
    assert reply_target is not None
    store.create_execution(
        "exec",
        ExecutionOrigin.WIRE,
        recipient.recipient_lookup,
        "owner",
        1,
        claim_ids=(claim.claim_id,),
        exact_target=reply_target,
    )
    store.mark_pending("exec", expected_revision=1)
    token = prepare_fence_token()
    first = store.start_attempt(
        "exec",
        1,
        "owner",
        1,
        token,
        expected_execution_revision=2,
        expected_pointer_revision=0,
    ).value.fence
    identity = None
    if retained:
        assert not process_dead
        store._test_retained_turns = True  # Explicit offline fixture; never ACP.
        identity = RetainedTurnIdentity(
            input_id="input-1",
            session_id="persistent-child-session",
            session_file=str(tmp_path / "saved-session.jsonl"),
            child_pid=12345,
            child_nonce="child-1",
            turn_id="turn-1",
            turn_generation=1,
            claim_id=claim.claim_id,
            claim_revision=store.snapshot("exec").claims[0].revision,
        )
        try:
            first = admit_test_retained_turn(store, comms.bus, root_id, first, identity).value.fence
        except BaseException:
            store.close()
            raise
    accepted_turn = store.advance_attempt(
        first, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=1
    ).value.fence
    model = store.advance_attempt(
        accepted_turn, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=1
    ).value.fence
    final = (
        store.advance_attempt(
            model,
            AttemptPhase.SETTLING,
            expected_pointer_revision=1,
            backend_done=True,
            process_dead=process_dead,
        ).value.fence
        if finalize
        else model
    )
    return Fixture(
        comms,
        bus,
        store,
        root_id,
        original.seq,
        reply_target,
        recipient.recipient_lookup,
        claim.claim_id,
        final,
        identity,
    )


def test_retained_child_cannot_publish_under_one_shot_process_death_contract(
    tmp_path: Path,
) -> None:
    """A live persistent child needs a new truthful turn-settled fact.

    Never claim process death to make a one-shot response gate pass.
    """
    case = _ready(tmp_path, direct=True, process_dead=False)
    try:
        with pytest.raises(RecoveryBlocked, match="death evidence"):
            prepare_fenced_response(case.store, case.bus, case.fence, "Not yet publishable")
        assert case.comms.bus.latest_sequence() == case.origin_seq
        assert case.store.snapshot("exec").obligation.state is ObligationState.PENDING
    finally:
        case.close()


def test_bounded_triage_engaged_claim_is_not_selected_full_admission(
    tmp_path: Path,
) -> None:
    with pytest.raises(StaleFence, match="original selected FULL"):
        _ready(tmp_path, direct=False, process_dead=False, retained=True)
    with MutationStore(str(tmp_path / "wire" / "coordination.sqlite3")) as store:
        snapshot = store.snapshot("exec")
        assert snapshot.claims[0].wake_mode is WakeMode.BOUNDED_TRIAGE
        assert snapshot.attempt.completion_kind == "one_shot"
        assert (
            store._connection.execute("SELECT COUNT(*) FROM retained_turn_receipts").fetchone()[0]
            == 0
        )


def test_retained_turn_requires_exact_receipt_then_uses_same_keyed_gateway(
    tmp_path: Path,
) -> None:
    case = _ready(tmp_path, direct=True, process_dead=False, retained=True)
    try:
        assert case.retained_identity is not None
        with pytest.raises(RecoveryBlocked, match="retained terminal receipt"):
            prepare_fenced_response(case.store, case.bus, case.fence, "Not settled")
        wrong = replace(case.retained_identity, input_id="different-input")
        with pytest.raises(StaleFence, match="retained terminal identity"):
            record_test_turn_settled(
                case.store, case.fence, RetainedTurnReceipt(wrong, 1, "a" * 64)
            )
        assert case.store.snapshot("exec").attempt.process_dead is False
        assert case.comms.bus.latest_sequence() == case.origin_seq
        settled = record_test_turn_settled(
            case.store,
            case.fence,
            RetainedTurnReceipt(case.retained_identity, 1, "a" * 64),
        ).value.fence
        assert case.store.snapshot("exec").attempt.process_dead is False
        with MutationStore(str(case.comms.root / "coordination.sqlite3")) as duplicate_connection:
            duplicate_connection._test_retained_turns = True
            duplicate = record_test_turn_settled(
                duplicate_connection,
                settled,
                RetainedTurnReceipt(case.retained_identity, 1, "a" * 64),
            )
            assert isinstance(duplicate, AlreadyApplied)
        with pytest.raises(RecoveryBlocked, match="conflicting retained receipt"):
            record_test_turn_settled(
                case.store,
                settled,
                RetainedTurnReceipt(case.retained_identity, 2, "b" * 64),
            )
        intent = prepare_fenced_response(
            case.store, case.bus, settled, "Exact retained response", timestamp=123.5
        ).value
        assert intent.exact_target == case.reply_target
        published = publish_fenced_response(case.store, case.bus, settled).value
        assert published.execution.status is ExecutionStatus.COMPLETED
        assert published.attempt.process_dead is False
        assert published.attempt.turn_settled is True
        assert case.comms.bus.latest_sequence() == case.origin_seq + 1
        assert isinstance(resolve_existing_response(case.store, case.bus, settled), AlreadyApplied)
        with MutationStore(str(case.comms.root / "coordination.sqlite3")) as reopened:
            with pytest.raises(RecoveryBlocked, match="replay is OFF"):
                resolve_existing_response(reopened, case.bus, settled)
            reopened._test_retained_turns = True
            assert isinstance(
                resolve_existing_response(reopened, case.bus, settled), AlreadyApplied
            )
    finally:
        case.close()


def test_death_after_committed_retained_receipt_does_not_erase_settlement(
    tmp_path: Path,
) -> None:
    case = _ready(tmp_path, direct=True, process_dead=False, retained=True)
    try:
        assert case.retained_identity is not None
        settled = record_test_turn_settled(
            case.store,
            case.fence,
            RetainedTurnReceipt(case.retained_identity, 1, "a" * 64),
        ).value.fence
        # The original owner is still live; only the already-settled Pi child
        # dies. This is NOT a permit to resume that child or replay raw input.
        dead = case.store.advance_attempt(
            settled,
            AttemptPhase.SETTLING,
            expected_pointer_revision=1,
            process_dead=True,
        ).value.fence
        attempt = case.store.snapshot("exec").attempt
        assert attempt.turn_settled and attempt.process_dead
        prepare_fenced_response(
            case.store,
            case.bus,
            dead,
            "Already settled before child death",
            timestamp=123.5,
        )
        published = publish_fenced_response(case.store, case.bus, dead).value
        assert published.execution.status is ExecutionStatus.COMPLETED
        assert published.attempt.turn_settled and published.attempt.process_dead
        assert case.comms.bus.latest_sequence() == case.origin_seq + 1
        assert isinstance(resolve_existing_response(case.store, case.bus, dead), AlreadyApplied)
    finally:
        case.close()


def test_out_of_order_retained_receipt_cannot_settle(tmp_path: Path) -> None:
    case = _ready(tmp_path, direct=True, process_dead=False, retained=True, finalize=False)
    try:
        assert case.retained_identity is not None
        with pytest.raises(RecoveryBlocked, match="truthful final backend"):
            record_test_turn_settled(
                case.store,
                case.fence,
                RetainedTurnReceipt(case.retained_identity, 1, "a" * 64),
            )
        with pytest.raises(RecoveryBlocked, match="final model/death evidence"):
            prepare_fenced_response(case.store, case.bus, case.fence, "too early")
        assert case.store.snapshot("exec").attempt.turn_settled is False
        assert case.comms.bus.latest_sequence() == case.origin_seq
    finally:
        case.close()


def test_retained_crash_cancel_and_late_receipt_do_not_publish(tmp_path: Path) -> None:
    case = _ready(tmp_path, direct=True, process_dead=False, retained=True)
    try:
        assert case.retained_identity is not None
        with pytest.raises(RecoveryBlocked, match="retained terminal receipt"):
            prepare_fenced_response(case.store, case.bus, case.fence, "crashed before receipt")
        # A child that actually died is not a retained successful turn.
        dead = case.store.advance_attempt(
            case.fence,
            AttemptPhase.SETTLING,
            expected_pointer_revision=1,
            process_dead=True,
        ).value.fence
        with pytest.raises(RecoveryBlocked, match="truthful final backend"):
            record_test_turn_settled(
                case.store,
                dead,
                RetainedTurnReceipt(case.retained_identity, 1, "a" * 64),
            )
        assert case.comms.bus.latest_sequence() == case.origin_seq
        with pytest.raises(RecoveryBlocked, match="one-shot final"):
            case.store.settle_nonpublication(
                dead,
                expected_pointer_revision=1,
                success=False,
                reason_code="child_lost",
            )
        # Reopening SQL does not manufacture a missing terminal receipt.
        with MutationStore(str(case.comms.root / "coordination.sqlite3")) as reopened:
            with pytest.raises(RecoveryBlocked, match="settlement is OFF"):
                prepare_fenced_response(reopened, case.bus, dead, "default off")
            reopened._test_retained_turns = True
            with pytest.raises(RecoveryBlocked, match="retained terminal receipt"):
                prepare_fenced_response(reopened, case.bus, dead, "still unknown")
            assert reopened.snapshot("exec").attempt.turn_settled is False
    finally:
        case.close()


def test_two_retained_turns_reuse_one_synthetic_child_identity(tmp_path: Path) -> None:
    case = _ready(tmp_path, direct=True, process_dead=False, retained=True)
    try:
        assert case.retained_identity is not None
        first = record_test_turn_settled(
            case.store,
            case.fence,
            RetainedTurnReceipt(case.retained_identity, 1, "a" * 64),
        ).value.fence
        prepare_fenced_response(case.store, case.bus, first, "first", timestamp=123.5)
        publish_fenced_response(case.store, case.bus, first)
        source = case.comms.send_initial_cohort("sender", "owner", "Second exact input")
        accepted = accept_initial_cohort(case.comms.bus, case.root_id, source.seq, case.store)
        claim = accepted.value.claims[0]
        target = derive_exact_reply_target(source)
        assert target is not None
        case.store.create_execution(
            "exec2",
            ExecutionOrigin.WIRE,
            case.owner_lookup,
            "owner",
            1,
            claim_ids=(claim.claim_id,),
            exact_target=target,
        )
        case.store.mark_pending("exec2", expected_revision=1)
        token = prepare_fence_token()
        fence = case.store.start_attempt(
            "exec2",
            1,
            "owner",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=2,
        ).value.fence
        second_id = replace(
            case.retained_identity,
            input_id="input-2",
            turn_id="turn-2",
            turn_generation=2,
            claim_id=claim.claim_id,
            claim_revision=case.store.snapshot("exec2").claims[0].revision,
        )
        with MutationStore(str(case.comms.root / "coordination.sqlite3")) as second_connection:
            second_connection._test_retained_turns = True
            with pytest.raises(IdentityConflict, match="already belongs"):
                admit_test_retained_turn(
                    second_connection,
                    case.comms.bus,
                    case.root_id,
                    fence,
                    replace(second_id, input_id=case.retained_identity.input_id),
                )
        assert case.store.snapshot("exec2").attempt.completion_kind == "one_shot"
        fence = admit_test_retained_turn(
            case.store, case.comms.bus, case.root_id, fence, second_id
        ).value.fence
        for phase in (
            AttemptPhase.PROMPT_ACCEPTED,
            AttemptPhase.MODEL_RUNNING,
            AttemptPhase.SETTLING,
        ):
            fence = case.store.advance_attempt(
                fence,
                phase,
                expected_pointer_revision=3,
                backend_done=(phase is AttemptPhase.SETTLING),
            ).value.fence
        fence = record_test_turn_settled(
            case.store, fence, RetainedTurnReceipt(second_id, 2, "b" * 64)
        ).value.fence
        prepare_fenced_response(case.store, case.bus, fence, "second", timestamp=124.5)
        result = publish_fenced_response(case.store, case.bus, fence).value
        assert result.execution.status is ExecutionStatus.COMPLETED
        assert result.attempt.process_dead is False
        receipts = list(
            case.store._connection.execute(
                "SELECT input_id,session_id,child_nonce FROM retained_turn_receipts "
                "ORDER BY input_id"
            )
        )
        assert [r["input_id"] for r in receipts] == ["input-1", "input-2"]
        assert len({(r["session_id"], r["child_nonce"]) for r in receipts}) == 1
    finally:
        case.close()


@pytest.mark.parametrize("direct", [False, True])
def test_real_bus_sql_tx1_exact_reply_tx2_and_lost_ack_replay(tmp_path: Path, direct: bool) -> None:
    case = _ready(tmp_path, direct=direct)
    try:
        result = prepare_fenced_response(
            case.store, case.bus, case.fence, "Response on the original route", timestamp=123.5
        )
        intent = result.value
        assert intent.exact_target == case.reply_target
        assert case.store.snapshot("exec").obligation.state is ObligationState.PUBLISHING
        assert case.bus.read_keyed_response(intent) is None
        with pytest.raises(PublicationUncertain):
            resolve_existing_response(case.store, case.bus, case.fence)
        assert case.comms.bus.latest_sequence() == case.origin_seq
        assert isinstance(
            prepare_fenced_response(
                case.store, case.bus, case.fence, intent.payload, timestamp=intent.timestamp
            ),
            AlreadyApplied,
        )
        published = publish_fenced_response(case.store, case.bus, case.fence).value
        assert published.execution.status is ExecutionStatus.COMPLETED
        assert published.obligation.state is ObligationState.PUBLISHED
        assert published.publication_receipt is not None
        assert published.claims[0].disposition is ClaimDisposition.COMPLETED
        assert published.publication_receipt.seq == case.origin_seq + 1
        response = case.bus.read_keyed_response(intent)
        assert response is not None and response.target == case.reply_target
        assert "_agent_comms_private_v1" not in response.to_wire()
        assert isinstance(
            resolve_existing_response(case.store, case.bus, case.fence), AlreadyApplied
        )
        assert published == case.store.snapshot("exec")
        case.store.close()
        with MutationStore(str(case.comms.root / "coordination.sqlite3")) as reopened:
            reread = resolve_existing_response(reopened, case.bus, case.fence)
            assert isinstance(reread, AlreadyApplied)
            assert reread.value.publication_receipt == published.publication_receipt
    finally:
        case.close()


def test_response_requires_explicit_writer_and_exact_same_root_coordinator(tmp_path: Path) -> None:
    case = _ready(tmp_path)
    try:
        disabled = MessageBus(case.comms.root / "bus.jsonl", case.comms.registry)
        with pytest.raises(PublicationActivationBlocked):
            prepare_fenced_response(case.store, disabled, case.fence, "not allowed")
        with (
            MutationStore(str(case.comms.root / "other.sqlite3")) as wrong_database,
            pytest.raises(IdentityConflict, match="different trusted roots"),
        ):
            prepare_fenced_response(wrong_database, case.bus, case.fence, "not allowed")
        alien_registry = ThreadRegistry(tmp_path / "foreign" / "registry.json")
        alien_bus = MessageBus(
            case.comms.root / "bus.jsonl", alien_registry, private_response_writes=True
        )
        with pytest.raises(IdentityConflict, match="different trusted roots"):
            prepare_fenced_response(case.store, alien_bus, case.fence, "not allowed")
        assert case.store.snapshot("exec").obligation.state is ObligationState.PENDING
        assert case.comms.bus.latest_sequence() == case.origin_seq
    finally:
        case.close()


def test_durable_dispatch_barrier_prevents_resend_after_crash_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(case.store, case.bus, case.fence, "reply").value

        def crash_before_append(_intent, *, registry_snapshot=None):
            raise OSError("injected process death before append")

        monkeypatch.setattr(case.bus, "_publish_keyed_response_unlocked", crash_before_append)
        with pytest.raises(OSError):
            publish_fenced_response(case.store, case.bus, case.fence)
        assert case.bus.read_keyed_response(intent) is None
        assert case.comms.bus.latest_sequence() == case.origin_seq
        assert (
            case.store._connection.execute(
                "SELECT COUNT(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 1
        )
        monkeypatch.undo()
        with pytest.raises(PublicationUncertain):
            publish_fenced_response(case.store, case.bus, case.fence)
        with pytest.raises(PublicationUncertain):
            resolve_existing_response(case.store, case.bus, case.fence)
        assert case.comms.bus.latest_sequence() == case.origin_seq
        assert case.store.snapshot("exec").obligation.state is ObligationState.PUBLISHING
    finally:
        case.close()


def test_lost_bus_ack_is_read_only_resolved_after_sql_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(case.store, case.bus, case.fence, "reply").value
        real_append = case.bus._publish_keyed_response_unlocked

        def committed_then_lost_ack(frozen, *, registry_snapshot=None):
            real_append(frozen, registry_snapshot=registry_snapshot)
            raise OSError("injected loss after bus fsync")

        monkeypatch.setattr(case.bus, "_publish_keyed_response_unlocked", committed_then_lost_ack)
        with pytest.raises(OSError):
            publish_fenced_response(case.store, case.bus, case.fence)
        assert case.bus.read_keyed_response(intent) is not None
        assert case.store.snapshot("exec").obligation.state is ObligationState.PUBLISHING
        monkeypatch.undo()
        case.store.close()
        with MutationStore(str(case.comms.root / "coordination.sqlite3")) as reopened:
            result = resolve_existing_response(reopened, case.bus, case.fence)
            assert result.value.obligation.state is ObligationState.PUBLISHED
            assert result.value.publication_receipt.seq == case.origin_seq + 1
            assert isinstance(
                publish_fenced_response(reopened, case.bus, case.fence), AlreadyApplied
            )
            assert case.comms.bus.latest_sequence() == case.origin_seq + 1
    finally:
        case.close()


def test_stale_owner_and_conflicting_payload_cannot_publish(tmp_path: Path) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(case.store, case.bus, case.fence, "reply").value
        with pytest.raises(IdentityConflict):
            prepare_fenced_response(case.store, case.bus, case.fence, "DIFFERENT")
        stale = replace(case.fence, revision=case.fence.revision - 1)
        with pytest.raises(StaleRevision):
            publish_fenced_response(case.store, case.bus, stale)
        assert case.bus.read_keyed_response(intent) is None
        assert (
            case.store._connection.execute(
                "SELECT COUNT(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 0
        )
    finally:
        case.close()


def test_bus_append_fence_remains_current_until_sql_tx2_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        prepare_fenced_response(case.store, case.bus, case.fence, "reply")
        entered, concurrent_done, wire_done = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        real_append = case.bus._publish_keyed_response_unlocked

        def blocking_append(frozen, *, registry_snapshot=None):
            entered.set()
            assert not concurrent_done.wait(0.25), "revocation raced an in-flight fenced append"
            assert not wire_done.is_set(), "registry writer raced an in-flight append"
            return real_append(frozen, registry_snapshot=registry_snapshot)

        monkeypatch.setattr(case.bus, "_publish_keyed_response_unlocked", blocking_append)
        outcomes: list[str] = []

        def revoke():
            assert entered.wait(2)
            try:
                with MutationStore(str(case.comms.root / "coordination.sqlite3")) as other:
                    other.advance_owner_generation(
                        case.owner_lookup, "new-owner", expected_generation=1
                    )
                outcomes.append("advanced")
            except sqlite3.IntegrityError:
                outcomes.append("blocked")  # active attempt may not change generation
            finally:
                concurrent_done.set()

        def change_registry_scope():
            assert entered.wait(2)
            with _store_lock(case.comms.root / "wire"):
                wire_done.set()

        worker = threading.Thread(target=revoke)
        wire_worker = threading.Thread(target=change_registry_scope)
        worker.start()
        wire_worker.start()
        try:
            settled = publish_fenced_response(case.store, case.bus, case.fence)
        finally:
            worker.join(timeout=5)
            wire_worker.join(timeout=5)
        assert not worker.is_alive() and outcomes == ["advanced"]
        assert not wire_worker.is_alive() and wire_done.is_set()
        assert settled.value.execution.status is ExecutionStatus.COMPLETED
        assert case.store.participant(case.owner_lookup).generation == 2
    finally:
        case.close()


def test_direct_registry_stop_in_other_process_waits_for_fenced_bus_and_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    child: subprocess.Popen[str] | None = None
    try:
        prepare_fenced_response(case.store, case.bus, case.fence, "reply")
        actual_append = case.bus._publish_keyed_response_unlocked
        env = os.environ.copy()
        for name in ("PI_AGENT_ID", "PI_PARENT_ID", "PI_AGENT_TAGS", "AGENT_COMMS_THREAD"):
            env.pop(name, None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

        def concurrent_stop(frozen, *, registry_snapshot=None):
            nonlocal child
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; from pathlib import Path; "
                    "from agent_comms.declarations import ThreadRegistry; "
                    "print('READY',flush=True); "
                    "ThreadRegistry(Path(sys.argv[1])).unregister('owner')",
                    str(case.comms.root / "registry.json"),
                ],
                cwd=case.comms.root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdout is not None
            with selectors.DefaultSelector() as watcher:
                watcher.register(child.stdout, selectors.EVENT_READ)
                assert watcher.select(5), "registry-stop process never reached its lock attempt"
            assert child.stdout.readline() == "READY\n"
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.15)
            return actual_append(frozen, registry_snapshot=registry_snapshot)

        monkeypatch.setattr(case.bus, "_publish_keyed_response_unlocked", concurrent_stop)
        result = publish_fenced_response(case.store, case.bus, case.fence)
        assert child is not None
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, (output, errors)
        assert not case.comms.registry.status("owner").active
        assert result.value.execution.status is ExecutionStatus.COMPLETED
        assert result.value.obligation.state is ObligationState.PUBLISHED
        assert case.comms.bus.latest_sequence() == case.origin_seq + 1
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        case.close()
