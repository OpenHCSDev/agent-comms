"""Actual fsynced bus + SQLite response boundaries, default OFF outside opted-in roots."""

from __future__ import annotations

from conftest import publish_initial, responder
import os
import selectors
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import CompletedAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import ModelRunningAttempt, PromptAcceptedAttempt, SettlingAttempt
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationUncertain,
    StaleRevision,
)
from agent_comms.coordination_response import (
    LiveResponseOwner,
    install_private_response_schema,
    prepare_fenced_response,
    publish_fenced_response,
    resolve_existing_response,
)
from agent_comms.coordination_results import AlreadyApplied
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordinator import Coordination
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.execution_states import CompletedExecution
from agent_comms.message_bus import MessageBus
from agent_comms.publisher import ParticipantPublisher
from agent_comms.obligation_states import PendingResponse, PublishedResponse, PublishingResponse
from agent_comms.owner_fence import prepare_fence_token
from agent_comms.registration import Registration
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.selected_source_batch import SelectedSource
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread

# Private bus publication requires POSIX owner/mode ancestry; Windows stat
# emulation cannot attest this boundary or exercise its durability contract.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus needs POSIX ownership")


@dataclass
class Fixture:
    comms: Comms
    bus: MessageBus[ParticipantPublisher]
    store: Coordination
    root_id: str
    origin_seq: int
    reply_target: str
    owner_lookup: str
    assignment_id: str
    fence: object
    witness: LiveResponseOwner

    def close(self) -> None:
        self.store.close()


def _ready(tmp_path: Path, *, direct: bool = False, extra_targets: tuple[str, ...] = ()) -> Fixture:
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "owner"):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset({'team'}), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    target = "owner" if direct else "#team"
    original = publish_initial(comms, 
        "sender", target, "Need owner to consider and reply."
    )
    original_record = comms.bus.log.read_delivery_cohort(root_id, original.seq)
    assert len(original_record.audience.recipients) == 1
    recipient = original_record.audience.recipients[0]
    bus = responder(comms.root, comms.registry)
    store = Coordination(str(comms.root / "coordination.sqlite3"), clock_ms=lambda: 1_000)
    install_private_cohort_schema(store)
    install_private_response_schema(store)
    store.participants.register(
        recipient.recipient_lookup,
        recipient.canonical_thread,
        recipient.canonical_thread,
        committed=True,
    )
    accepted = accept_delivery_cohort(comms.bus, root_id, original.seq, store)
    assert accepted.value.member_count == accepted.value.assignment_count == 1
    assignment = accepted.value.assignments[0]
    sources = [SelectedSource(assignment.assignment_id, store.assignments, original_record)]
    for extra_target in extra_targets:
        message = publish_initial(comms, "sender", extra_target, "Another original question")
        delivery = comms.bus.log.read_delivery_cohort(root_id, message.seq)
        acknowledgement = accept_delivery_cohort(comms.bus, root_id, message.seq, store).value
        (extra_assignment,) = acknowledgement.assignments
        sources.append(SelectedSource(extra_assignment.assignment_id, store.assignments, delivery))
    reply_target = original.exact_reply_target
    assert reply_target is not None
    store.executions.create(
        "exec",
        ExecutionOrigin.WIRE,
        recipient.recipient_lookup,
        "owner",
        1,
        sources=tuple(sources),
    )
    store.executions.mark_pending("exec", expected_revision=1)
    token = prepare_fence_token()
    first = store.attempts.start(
        AttemptStart(
            "exec",
            1,
            "owner",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
    ).value.fence
    accepted_turn = store.attempts.advance(
        first, PromptAcceptedAttempt, expected_pointer_revision=1
    ).value.fence
    model = store.attempts.advance(
        accepted_turn, ModelRunningAttempt, expected_pointer_revision=1
    ).value.fence
    final = store.attempts.advance(
        model,
        SettlingAttempt,
        expected_pointer_revision=1,
        backend_done=True,
        process_dead=True,
    ).value.fence
    owner, claim_scope = comms.registry.live_owner_with_claim("owner")
    owner, claim_scope = comms.registry.lease_live_turn_with_claim(
        owner, "fixture-response-turn", expected_generation=claim_scope
    )
    witness = LiveResponseOwner(thread=owner, claimed_generation=claim_scope)
    return Fixture(
        comms,
        bus,
        store,
        root_id,
        original.seq,
        reply_target,
        recipient.recipient_lookup,
        assignment.assignment_id,
        final,
        witness,
    )


def test_original_routes_prepare_together_and_release_one_attempt_only_after_all_receipts(tmp_path: Path):
    case = _ready(tmp_path, extra_targets=("owner",))
    try:
        batch = case.store.snapshots.get("exec")
        routes = tuple(row.exact_target for row in batch.require_wire_responses())
        assert set(routes) == {"#team", "sender"}
        assert len(batch.assignments) == 2
        first = prepare_fenced_response(
            case.store, case.bus, case.fence, "Channel questions combined",
            exact_target=routes[0], owner_witness=case.witness,
        ).value
        with pytest.raises(IdentityConflict, match="all original response bodies"):
            publish_fenced_response(
                case.store, case.bus, case.fence,
                exact_target=routes[0], owner_witness=case.witness,
            )
        assert not case.store.snapshots.get("exec").publication_evidence
        assert case.bus.log.read_keyed_response(first) is None
        prepare_fenced_response(
            case.store, case.bus, case.fence, "Direct questions combined",
            exact_target=routes[1], owner_witness=case.witness,
        )
        partial = publish_fenced_response(
            case.store, case.bus, case.fence,
            exact_target=routes[0], owner_witness=case.witness,
        ).value
        assert partial.execution.lifecycle.active and partial.is_current
        assert len(partial.publication_evidence) == 1
        assert all(row.lifecycle.require_engagement() for row in partial.assignments)
        settled = publish_fenced_response(
            case.store, case.bus, case.fence,
            exact_target=routes[1], owner_witness=case.witness,
        ).value
        assert settled.execution.lifecycle.completed and not settled.is_current
        assert len(settled.publication_evidence) == 2
        assert all(row.lifecycle.require_completion() for row in settled.assignments)
        assert settled.current_execution_id is None
        for route in routes:
            intent, acknowledgement = settled.require_published_evidence(route)
            message = case.bus.log.read_keyed_response(intent)
            assert message.reference == acknowledgement.reference and message.target == route
            assert message.answers == tuple(
                assignment.source for assignment in settled.assignments
                if assignment.lifecycle.exact_target == route
            )
    finally:
        case.close()


@pytest.mark.parametrize("direct", [False, True])
def test_real_bus_sql_tx1_exact_reply_tx2_and_lost_ack_replay(tmp_path: Path, direct: bool) -> None:
    case = _ready(tmp_path, direct=direct)
    try:
        result = prepare_fenced_response(
            case.store,
            case.bus,
            case.fence,
            "Response on the original route",
            timestamp=123.5,
            exact_target=case.reply_target, owner_witness=case.witness,
        )
        intent = result.value
        assert intent.exact_target == case.reply_target
        assert type(case.store.snapshots.get("exec").require_wire_response(case.reply_target).lifecycle) is PublishingResponse
        assert case.bus.log.read_keyed_response(intent) is None
        with pytest.raises(PublicationUncertain):
            resolve_existing_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        assert case.comms.bus.log.latest_sequence() == case.origin_seq
        assert isinstance(
            prepare_fenced_response(
                case.store,
                case.bus,
                case.fence,
                intent.message.body,
                timestamp=intent.message.timestamp,
                exact_target=case.reply_target, owner_witness=case.witness,
            ),
            AlreadyApplied,
        )
        published = publish_fenced_response(
            case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness
        ).value
        assert type(published.execution.lifecycle) is CompletedExecution
        assert type(published.require_wire_response(case.reply_target).lifecycle) is PublishedResponse
        assert ResponseObligation.response_record(published.publication_evidence, case.reply_target) is not None
        assert type(published.assignments[0].lifecycle) is CompletedAssignment
        assert ResponseObligation.response_record(published.publication_evidence, case.reply_target).seq == case.origin_seq + 1
        response = case.bus.log.read_keyed_response(intent)
        assert response is not None and response.target == case.reply_target
        assert "_agent_comms_private_v1" not in response.to_wire()
        assert isinstance(
            resolve_existing_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness),
            AlreadyApplied,
        )
        assert published == case.store.snapshots.get("exec")
        case.store.close()
        with Coordination(str(case.comms.root / "coordination.sqlite3")) as reopened:
            reread = resolve_existing_response(
                reopened, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness
            )
            assert isinstance(reread, AlreadyApplied)
            assert ResponseObligation.response_record(reread.value.publication_evidence, case.reply_target) == ResponseObligation.response_record(published.publication_evidence, case.reply_target)
    finally:
        case.close()


def test_response_requires_exact_same_root_coordinator(tmp_path: Path) -> None:
    case = _ready(tmp_path)
    try:
        with (
            Coordination(str(case.comms.root / "other.sqlite3")) as wrong_database,
            pytest.raises(IdentityConflict, match="different trusted roots"),
        ):
            prepare_fenced_response(
                wrong_database, case.bus, case.fence, "not allowed", exact_target=case.reply_target, owner_witness=case.witness
            )
        alien_registry = Registration(tmp_path / "foreign" / "registry.json")
        alien_bus = responder(case.comms.root, alien_registry)
        with pytest.raises(IdentityConflict, match="different trusted roots"):
            prepare_fenced_response(
                case.store, alien_bus, case.fence, "not allowed", exact_target=case.reply_target, owner_witness=case.witness
            )
        assert type(case.store.snapshots.get("exec").require_wire_response(case.reply_target).lifecycle) is PendingResponse
        assert case.comms.bus.log.latest_sequence() == case.origin_seq
    finally:
        case.close()


def test_durable_dispatch_barrier_prevents_resend_after_crash_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "reply", exact_target=case.reply_target, owner_witness=case.witness
        ).value

        def crash_before_append(_intent, *, conversation, registry_snapshot=None, activity):
            raise OSError("injected process death before append")

        monkeypatch.setattr(
            case.bus.publisher, "_publish_keyed_response_unlocked", crash_before_append
        )
        with pytest.raises(OSError):
            publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        assert case.bus.log.read_keyed_response(intent) is None
        assert case.comms.bus.log.latest_sequence() == case.origin_seq
        assert (
            case.store.session._connection.execute(
                "SELECT COUNT(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 1
        )
        monkeypatch.undo()
        with pytest.raises(PublicationUncertain):
            publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        with pytest.raises(PublicationUncertain):
            resolve_existing_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        with pytest.raises(PublicationUncertain):
            case.witness.resolve_retained(case.store, case.bus)
        assert case.comms.bus.log.latest_sequence() == case.origin_seq
        assert type(case.store.snapshots.get("exec").require_wire_response(case.reply_target).lifecycle) is PublishingResponse
    finally:
        case.close()


def test_lost_bus_ack_is_read_only_resolved_after_sql_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "reply", exact_target=case.reply_target, owner_witness=case.witness
        ).value
        real_append = case.bus.publisher._publish_keyed_response_unlocked

        def committed_then_lost_ack(frozen, *, conversation, registry_snapshot=None, activity):
            real_append(frozen, conversation=conversation, registry_snapshot=registry_snapshot,
                        activity=activity)
            raise OSError("injected loss after bus fsync")

        monkeypatch.setattr(
            case.bus.publisher, "_publish_keyed_response_unlocked", committed_then_lost_ack
        )
        with pytest.raises(OSError):
            publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        assert case.bus.log.read_keyed_response(intent) is not None
        assert type(case.store.snapshots.get("exec").require_wire_response(case.reply_target).lifecycle) is PublishingResponse
        monkeypatch.undo()
        case.store.close()
        with Coordination(str(case.comms.root / "coordination.sqlite3")) as reopened:
            completed = case.witness.resolve_retained(reopened, case.bus)
            assert len(completed) == 1
            assert completed[0].publication_evidence[0].seq == case.origin_seq + 1
            result = resolve_existing_response(
                reopened, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness
            )
            assert type(result.value.require_wire_response(case.reply_target).lifecycle) is PublishedResponse
            assert ResponseObligation.response_record(result.value.publication_evidence, case.reply_target).seq == case.origin_seq + 1
            assert isinstance(
                publish_fenced_response(reopened, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness),
                AlreadyApplied,
            )
            assert case.comms.bus.log.latest_sequence() == case.origin_seq + 1
    finally:
        case.close()


def test_fresh_selection_completes_saved_reply_before_admitting_new_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "saved reply",
            exact_target=case.reply_target, owner_witness=case.witness,
        ).value
        append = case.bus.publisher._publish_keyed_response_unlocked

        def saved_then_lost(frozen, *, conversation, registry_snapshot=None, activity):
            append(frozen, conversation=conversation, registry_snapshot=registry_snapshot,
                   activity=activity)
            raise OSError("lost acknowledgement after saved reply")

        monkeypatch.setattr(
            case.bus.publisher, "_publish_keyed_response_unlocked", saved_then_lost
        )
        with pytest.raises(OSError, match="lost acknowledgement"):
            publish_fenced_response(
                case.store, case.bus, case.fence,
                exact_target=case.reply_target, owner_witness=case.witness,
            )
        assert case.bus.log.read_keyed_response(intent) is not None
        monkeypatch.undo()
        case.comms.agents.finish_turn(case.witness.thread.require_turn_lease())
        fresh = publish_initial(case.comms, 
            "sender", "#team", "Distinct fresh work"
        )
        accepted = accept_delivery_cohort(case.comms.bus, case.root_id, fresh.seq, case.store)
        fresh_assignment = accepted.value.assignments[0]
        install_native_runtime_schema(case.store)

        _owner, participant, pending, _sent_outcomes = SelectedParticipant.prepare(
            case.comms, case.bus, case.store, case.root_id, "owner", case.origin_seq
        )

        settled = case.store.snapshots.get("exec")
        assert type(settled.require_wire_response(case.reply_target).lifecycle) is PublishedResponse
        assert participant.pointer.execution_id is None
        assert tuple(row.assignment_id for row in pending) == (fresh_assignment.assignment_id,)
        assert case.comms.bus.log.latest_sequence() == fresh.seq
    finally:
        case.close()


def test_fresh_selection_refuses_missing_saved_reply_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "missing saved reply",
            exact_target=case.reply_target, owner_witness=case.witness,
        ).value

        def lost_before_append(_intent, *, conversation, registry_snapshot=None, activity):
            raise OSError("reply was never saved")

        monkeypatch.setattr(
            case.bus.publisher, "_publish_keyed_response_unlocked", lost_before_append
        )
        with pytest.raises(OSError, match="never saved"):
            publish_fenced_response(
                case.store, case.bus, case.fence,
                exact_target=case.reply_target, owner_witness=case.witness,
            )
        assert case.bus.log.read_keyed_response(intent) is None
        monkeypatch.undo()
        case.comms.agents.finish_turn(case.witness.thread.require_turn_lease())
        fresh = publish_initial(case.comms, 
            "sender", "#team", "Fresh work must remain blocked"
        )
        accept_delivery_cohort(case.comms.bus, case.root_id, fresh.seq, case.store)
        install_native_runtime_schema(case.store)

        with pytest.raises(PublicationUncertain, match="no keyed bus acknowledgement; no resend"):
            SelectedParticipant.prepare(
                case.comms, case.bus, case.store, case.root_id, "owner", case.origin_seq
            )
        assert case.bus.log.read_keyed_response(intent) is None
        assert case.comms.bus.log.latest_sequence() == fresh.seq
        assert case.store.snapshots.get("exec").publication_evidence == ()
    finally:
        case.close()


def test_stale_owner_and_conflicting_payload_cannot_publish(tmp_path: Path) -> None:
    case = _ready(tmp_path)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "reply", exact_target=case.reply_target, owner_witness=case.witness
        ).value
        with pytest.raises(IdentityConflict):
            prepare_fenced_response(
                case.store, case.bus, case.fence, "DIFFERENT", exact_target=case.reply_target, owner_witness=case.witness
            )
        stale = replace(case.fence, revision=case.fence.revision - 1)
        with pytest.raises(StaleRevision):
            publish_fenced_response(case.store, case.bus, stale, exact_target=case.reply_target, owner_witness=case.witness)
        assert case.bus.log.read_keyed_response(intent) is None
        assert (
            case.store.session._connection.execute(
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
        prepare_fenced_response(
            case.store, case.bus, case.fence, "reply", exact_target=case.reply_target, owner_witness=case.witness
        )
        entered, concurrent_done, wire_done = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        real_append = case.bus.publisher._publish_keyed_response_unlocked

        def blocking_append(frozen, *, conversation, registry_snapshot=None, activity):
            entered.set()
            assert not concurrent_done.wait(0.25), "revocation raced an in-flight fenced append"
            assert not wire_done.is_set(), "registry writer raced an in-flight append"
            return real_append(
                frozen, conversation=conversation, registry_snapshot=registry_snapshot,
                activity=activity,
            )

        monkeypatch.setattr(case.bus.publisher, "_publish_keyed_response_unlocked", blocking_append)
        outcomes: list[str] = []

        def revoke():
            assert entered.wait(2)
            try:
                with Coordination(str(case.comms.root / "coordination.sqlite3")) as other:
                    other.participants.advance_generation(
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
            settled = publish_fenced_response(
                case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness
            )
        finally:
            worker.join(timeout=5)
            wire_worker.join(timeout=5)
        assert not worker.is_alive() and outcomes == ["advanced"]
        assert not wire_worker.is_alive() and wire_done.is_set()
        assert type(settled.value.execution.lifecycle) is CompletedExecution
        assert case.store.participants.get(case.owner_lookup).participant_generation == 2
    finally:
        case.close()


def test_direct_registry_stop_in_other_process_waits_for_fenced_bus_and_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _ready(tmp_path)
    child: subprocess.Popen[str] | None = None
    try:
        prepare_fenced_response(
            case.store, case.bus, case.fence, "reply", exact_target=case.reply_target, owner_witness=case.witness
        )
        actual_append = case.bus.publisher._publish_keyed_response_unlocked
        env = os.environ.copy()
        for name in ("PI_PARENT_ID", "PI_AGENT_TAGS", "AGENT_COMMS_THREAD"):
            env.pop(name, None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

        def concurrent_stop(frozen, *, conversation, registry_snapshot=None, activity):
            nonlocal child
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; from pathlib import Path; "
                    "from agent_comms.registration import Registration; "
                    "print('READY',flush=True); "
                    "Registration(Path(sys.argv[1])).unregister('owner')",
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
            return actual_append(
                frozen, conversation=conversation, registry_snapshot=registry_snapshot,
                activity=activity,
            )

        monkeypatch.setattr(case.bus.publisher, "_publish_keyed_response_unlocked", concurrent_stop)
        result = publish_fenced_response(
            case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness
        )
        assert child is not None
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, (output, errors)
        assert not case.comms.registry.status("owner").active
        assert type(result.value.execution.lifecycle) is CompletedExecution
        assert type(result.value.require_wire_response(case.reply_target).lifecycle) is PublishedResponse
        assert case.comms.bus.log.latest_sequence() == case.origin_seq + 1
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        case.close()


@pytest.mark.parametrize("change", ["finish", "stop", "pid", "pid_reuse", "turn", "recipient"])
def test_response_requires_current_exact_live_turn(tmp_path, change):
    """No PID-only or omitted-witness path can turn SQL finality into publication."""
    from agent_comms.coordination_errors import StaleFence

    case = _ready(tmp_path)
    try:
        witness = case.witness
        if change == "finish":
            owner = case.comms.registry.require("owner")
            case.comms.agents.finish_turn(owner.turn_lease)
        elif change == "stop":
            case.comms.registry.unregister("owner")
        elif change == "pid":
            witness = replace(witness, thread=replace(
                witness.thread, process_identity=ProcessIdentity(os.getpid() + 1, 1)
            ))
        elif change == "pid_reuse":
            process = witness.thread.process_identity
            witness = replace(witness, thread=replace(
                witness.thread, process_identity=ProcessIdentity(process.pid, process.start_time + 1)
            ))
        elif change == "turn":
            witness = replace(witness, thread=witness.thread.without_turn())
        else:
            witness = replace(witness, thread=replace(witness.thread, created_at=witness.thread.created_at + 1))
        with pytest.raises(StaleFence):
            prepare_fenced_response(
                case.store, case.bus, case.fence, "must not publish",
                exact_target=case.reply_target, owner_witness=witness,
            )
        assert case.bus.log.latest_sequence() == case.origin_seq
        assert ResponseObligation.response_record(case.store.snapshots.get("exec").publication_intents, case.reply_target) is None
    finally:
        case.close()


def test_response_answers_the_messages_its_turn_took_in(tmp_path: Path) -> None:
    from uuid import uuid4

    from agent_comms.input_disposition import InputDispositions
    from agent_comms.input_origin import WireInputOrigin

    case = _ready(tmp_path, direct=True)
    try:
        follow_up = case.comms.messaging.send("sender", "owner", "Part 2, mid-turn").message
        lease = case.witness.thread.require_turn_lease()
        inputs = InputDispositions(case.comms.root)
        key = inputs.bus_key(follow_up, case.witness.thread)
        text = "Part 2, mid-turn"
        assert inputs.record(
            key, seq=follow_up.seq, owner="owner", claim_scope=lease.claimed_generation,
            target="owner", text=text, origin=WireInputOrigin(case.root_id, follow_up.reference),
        )
        native_id = uuid4().hex
        assert inputs.bind(
            key, claim_scope=lease.claimed_generation, turn_id=lease.turn_id,
            native_id=native_id, text=text,
        )
        assert inputs.started(key, turn_id=lease.turn_id, native_id=native_id, text=text)

        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "Answers both",
            exact_target=case.reply_target, owner_witness=case.witness,
        ).value
        publish_fenced_response(
            case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness
        )
        response = case.bus.log.read_keyed_response(intent)
        original = case.bus.log.read_delivery_cohort(case.root_id, case.origin_seq)
        assert response.answers == (original.message.reference, follow_up.reference)
        with case.bus.log.checked_read() as source:
            replies = tuple(source.replies((original.message.reference, follow_up.reference)))
        assert [reply.message.reference for reply in replies] == [response.reference] * 2
    finally:
        case.close()
