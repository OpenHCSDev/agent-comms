"""Original committed raw row → complete atomic N/K acceptance, no live wake."""

from __future__ import annotations

from bus_history import read_history
from conftest import declare_test_bus, publish_initial
import hashlib
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.assignment_states import (
    DeferredAssignment,
    DeliveredAssignment,
    FullPendingAssignment,
)
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD
from agent_comms.cohort_schema import CohortDeliveryReceipts, install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import (
    accept_delivery_cohort,
    pending_sealed_assignments,
    sealed_cohort_assignments,
)
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    canonical_publication_key,
)
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.exporting import (
    ChannelScope,
    FullLimit,
    JsonlFormat,
)
from agent_comms.messages import InfoKind, Message
from agent_comms.thread_identity import UserThreadRole
from agent_comms.thread_status import ArchivedThreadStatus
from agent_comms.threads import Thread
from agent_comms.wake_policy import BoundedTriageWake, FullWake, MessageAudience
from agent_comms.message_page import PageBounds

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="private initial bus requires POSIX owner/directory durability"
)


def _root(tmp_path: Path) -> tuple[Comms, Coordination, str, dict[str, str]]:
    root = tmp_path / "comms"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    people = {
        "sender": (17001.0, {"team"}),
        "Alice": (17002.0, {"team"}),
        "Bob": (17003.0, {"team"}),
        "Charlie": (17004.0, {"other"}),
    }
    declare_test_bus(comms, channels=("team", "other"), threads=(
        comms.registry.new_thread(name=name, worktree=str(root), created_at=created_at, tags=frozenset(tags))
        for name, (created_at, tags) in people.items()
    ))
    lookups = {
        name: RecordedThreadIncarnation("fixture", created_at).lookup
        for name, (created_at, _tags) in people.items()
    }
    coordinator = Coordination(str(root / "coordinator.sqlite"), clock_ms=lambda: 4723)
    install_private_cohort_schema(coordinator)
    for name in ("Alice", "Bob", "Charlie"):
        coordinator.participants.register(lookups[name], name, name, committed=True)
    root_id = comms.messaging.initialize_private_initial_protocol()
    return comms, coordinator, root_id, lookups


def test_private_initial_opt_in_full_n_observer_and_exact_public_projection(tmp_path: Path) -> None:
    comms, coordinator, root_id, lookups = _root(tmp_path)
    assert len(root_id) == 32
    assert (tmp_path / "comms" / "bus_meta.json").stat().st_mode & 0o777 == 0o600
    message = publish_initial(comms, "sender", "#team", "Hello @Alice")
    raw = json.loads((tmp_path / "comms" / "bus.jsonl").read_text())
    assert (tmp_path / "comms" / "bus.jsonl").stat().st_mode & 0o777 == 0o600
    assert set(raw[PRIVATE_WIRE_FIELD]) == {"kind", "version", "initial"}
    assert raw[PRIVATE_WIRE_FIELD]["kind"] == "initial"
    assert raw["seq"] == message.seq and raw["id"] == message.message_id
    assert all(PRIVATE_WIRE_FIELD not in item.to_wire() for item in read_history(comms))
    assert [item.seq for item in read_history(comms, bounds=PageBounds(max_bytes=32))] == [
        message.seq
    ]
    verified = comms.bus.log.read_delivery_cohort(root_id, message.seq)
    assert [r.recipient_lookup for r in verified.audience.recipients] == sorted(
        (lookups["Alice"], lookups["Bob"])
    )
    assert len(verified.decisions) == 2
    assert [type(decision).__name__ for decision in verified.decisions].count("NoWakeDecision") == 1
    first = accept_delivery_cohort(comms.bus, root_id, message.seq, coordinator)
    assert isinstance(first, Applied)
    assert (first.value.member_count, first.value.assignment_count) == (2, 1)
    assert first.value.assignments[0].recipient_lookup == lookups["Alice"]
    assert first.value.assignments[0].lifecycle.mode == FullWake()
    assert first.value.accepted_at_ms == 4723
    assert len(sealed_cohort_assignments(coordinator, lookups["Alice"])) == 1
    assert sealed_cohort_assignments(coordinator, lookups["Bob"]) == ()
    delivery_rows = coordinator.session._connection.execute(
        "SELECT kind,claim_id FROM cohort_delivery_receipts ORDER BY ordinal"
    ).fetchall()
    assert {row["kind"] for row in delivery_rows} == {"selected", "unmentioned_observer"}
    assert sum(row["claim_id"] is None for row in delivery_rows) == 1


def test_initial_sideband_never_enters_export_or_public_page_budget(tmp_path: Path) -> None:
    comms, _coordinator, _root_id, _lookups = _root(tmp_path)
    first = publish_initial(comms, "sender", "#team", "first @Alice")
    second = publish_initial(comms, "sender", "#team", "second @Bob")
    budget = sum(len(json.dumps(message.to_wire()).encode()) + 1 for message in (first, second))
    page = read_history(comms, bounds=PageBounds(max_bytes=budget))
    assert [item.seq for item in page] == [first.seq, second.seq]
    output = tmp_path / "public.jsonl"
    comms.views.export_wire(
        output,
        format=JsonlFormat(),
        scope=ChannelScope("#team"),
        limit=FullLimit(),
        export_started_at=time.time() + 5,
    )
    assert PRIVATE_WIRE_FIELD not in output.read_text()
    assert [
        record["message"]["text"] for record in map(json.loads, output.read_text().splitlines()[1:])
    ] == ["first @Alice", "second @Bob"]


def test_crash_before_sql_and_lost_ack_replay_after_rename_and_tags(tmp_path: Path) -> None:
    comms, coordinator, root_id, lookups = _root(tmp_path)
    message = publish_initial(comms, "sender", "#team", "all must respond")
    # Full bus fsync occurred; coordinator process crashes before BEGIN IMMEDIATE.
    coordinator.close()
    reopened = Comms(tmp_path / "comms")
    reopened.registry.rename("Alice", "Alicia")
    reopened.registry.register(replace(reopened.registry.require("Bob"), tags=frozenset({"other"})))
    store = Coordination(str(tmp_path / "comms" / "coordinator.sqlite"), clock_ms=lambda: 4788)
    result = accept_delivery_cohort(reopened.bus, root_id, message.seq, store)
    assert isinstance(result, Applied)
    assert (result.value.member_count, result.value.assignment_count) == (2, 2)
    assert {assignment.recipient for assignment in result.value.assignments} == {"Alice", "Bob"}
    assert {assignment.audience for assignment in result.value.assignments} == {
        MessageAudience.COLLECTIVE
    }
    store.assignments.transition_preengagement(
        result.value.assignments[0].assignment_id, DeferredAssignment, expected_revision=1
    )
    store.close()
    # The SQL COMMIT succeeded, but the client lost its reply. No reappend or
    # restamping is allowed, even after dispositions and registration evolve.
    recovered = Coordination(str(tmp_path / "comms" / "coordinator.sqlite"), clock_ms=lambda: 9000)
    again = accept_delivery_cohort(reopened.bus, root_id, message.seq, recovered)
    assert isinstance(again, AlreadyApplied)
    assert again.value.accepted_at_ms == 4788
    assert any(
        type(assignment.lifecycle) is DeferredAssignment for assignment in again.value.assignments
    )
    assert [assignment.recipient_lookup for assignment in again.value.assignments] == [
        assignment.recipient_lookup for assignment in result.value.assignments
    ]
    assert {assignment.recipient_lookup for assignment in again.value.assignments} == {
        lookups["Alice"],
        lookups["Bob"],
    }
    assert (
        recovered.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 2
    )


def test_sealed_receipts_follow_recipient_progress(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    alice_first = publish_initial(comms, "sender", "Alice", "first")
    bob = publish_initial(comms, "sender", "Bob", "other recipient")
    alice_second = publish_initial(comms, "sender", "Alice", "second")
    for message in (alice_first, bob, alice_second):
        accept_delivery_cohort(comms.bus, root_id, message.seq, store)

    assert CohortDeliveryReceipts.sealed_sequences_after(
        store.session._connection,
        wire_root_id=root_id,
        recipient_lookup=lookups["Alice"],
        after_seq=alice_first.seq,
    ) == frozenset({alice_second.seq})
    assert CohortDeliveryReceipts.sealed_sequences_after(
        store.session._connection,
        wire_root_id=root_id,
        recipient_lookup=lookups["Bob"],
        after_seq=0,
    ) == frozenset({bob.seq})


def test_zero_member_zero_claim_and_direct_message(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    declare_test_bus(comms, channels=("empty",))
    empty = publish_initial(comms, "sender", "#empty", "No recipient")
    result = accept_delivery_cohort(comms.bus, root_id, empty.seq, store)
    assert isinstance(result, Applied)
    assert (result.value.member_count, result.value.assignment_count, result.value.assignments) == (
        0,
        0,
        (),
    )
    assert isinstance(accept_delivery_cohort(comms.bus, root_id, empty.seq, store), AlreadyApplied)
    direct = publish_initial(comms, "sender", "Alice", "Direct")
    single = accept_delivery_cohort(comms.bus, root_id, direct.seq, store)
    assert isinstance(single, Applied)
    assert (single.value.member_count, single.value.assignment_count) == (1, 1)
    assert single.value.assignments[0].recipient_lookup == lookups["Alice"]
    assert single.value.assignments[0].audience is MessageAudience.DIRECT
    comms.registry.rename("Alice", "Alicia")
    assert accept_delivery_cohort(comms.bus, root_id, direct.seq, store).value == single.value


def test_legacy_preexisting_claim_blocks_entire_batch_and_rollback(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = publish_initial(comms, "sender", "#team", "Hello @Alice")
    legacy = WakeAssignment(
        assignment_id="singleton-before-cohort",
        recipient="Alice",
        recipient_lookup=lookups["Alice"],
        wire_seq=sent.seq,
        message_id=sent.message_id,
        audience=MessageAudience.MENTIONED,
        accepted_at_ms=1,
        updated_at_ms=1,
        lifecycle=FullPendingAssignment.load(FullWake(), None, None, None),
    )
    store.assignments.accept(legacy)
    with pytest.raises(IdentityConflict, match="singleton"):
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    assert (
        store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
        == 0
    )
    assert (
        store.session._connection.execute(
            "SELECT COUNT(*) FROM cohort_delivery_receipts"
        ).fetchone()[0]
        == 0
    )
    assert sealed_cohort_assignments(store, lookups["Alice"]) == ()


def test_no_wake_observer_cannot_gain_legacy_claim_after_seal(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = publish_initial(comms, "sender", "#team", "@Alice hello")
    result = accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    assert isinstance(result, Applied)
    observer_claim = replace(
        result.value.assignments[0],
        assignment_id="legacy-observer",
        recipient="Bob",
        recipient_lookup=lookups["Bob"],
    )
    with pytest.raises(sqlite3.IntegrityError, match="no-wake observer"):
        store.assignments.accept(observer_claim)
    assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 1


def test_corrupt_initial_and_wrong_root_rejected_before_sql(tmp_path: Path) -> None:
    comms, store, root_id, _lookups = _root(tmp_path)
    sent = publish_initial(comms, "sender", "#team", "Hello @Alice")
    with pytest.raises(IdentityConflict, match="root"):
        accept_delivery_cohort(comms.bus, "a" * 32, sent.seq, store)
    bus_path = tmp_path / "comms" / "bus.jsonl"
    original = bus_path.read_bytes()
    row = json.loads(original)
    row[PRIVATE_WIRE_FIELD]["initial"]["decisions"][0]["recipient_lookup"] = "imposter"
    bus_path.write_text(json.dumps(row) + "\n")
    with pytest.raises(
        RelationViolationError, match="Private bus checkpoint root/size changed"
    ):
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    assert (
        store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
        == 0
    )
    bus_path.write_bytes(original[:-1])
    with pytest.raises(
        RelationViolationError, match="Private bus checkpoint root/size changed"
    ):
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    bus_path.write_bytes(original)


def test_keyed_response_replay_after_initial_row_and_receipt_backed_page(tmp_path: Path) -> None:
    from agent_comms.coordination_response import prepare_fenced_response, publish_fenced_response
    from test_coordination_response import _ready

    case = _ready(tmp_path, direct=True)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "reply",
            exact_target=case.reply_target, owner_witness=case.witness,
        ).value
        publish_fenced_response(
            case.store, case.bus, case.fence,
            exact_target=case.reply_target, owner_witness=case.witness,
        )
        first = case.bus.log.read_keyed_response(intent)
        publish_fenced_response(
            case.store, case.bus, case.fence,
            exact_target=case.reply_target, owner_witness=case.witness,
        )
        assert case.bus.log.read_keyed_response(intent) == first
        assert [m.seq for m in read_history(case.comms)] == [case.origin_seq, first.seq]
        delivery = case.bus.log.read_delivery_cohort(case.root_id, first.seq)
        assert delivery.audience.canonical_members == {"sender"}
        assert delivery.decisions[0].wake_mode.triage
    finally:
        case.close()


def test_all_channel_excludes_sender_and_nonexecutors(
    tmp_path: Path,
) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    comms.registry.register(
        comms.registry.new_thread(name='human', tags=frozenset(), worktree=str(tmp_path), role=UserThreadRole, created_at=17005.0)
    )
    sent = publish_initial(comms, "sender", "#all", "ordinary")
    result = accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    assert isinstance(result, Applied)
    assert (result.value.member_count, result.value.assignment_count) == (3, 3)
    assert {assignment.recipient_lookup for assignment in result.value.assignments} == {
        lookups["Alice"],
        lookups["Bob"],
        lookups["Charlie"],
    }
    assert {assignment.lifecycle.mode for assignment in result.value.assignments} == {
        BoundedTriageWake()
    }


def test_wrong_and_partial_db_receipt_never_accepts_one_n_member(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = publish_initial(comms, "sender", "#team", "Hello @Alice")
    proof = comms.bus.log.read_delivery_cohort(root_id, sent.seq)
    with store.session.writing() as db:
        db.execute(
            "INSERT INTO claim_batch_receipts (wire_root_id,wire_seq,message_id,exact_target,"
            "envelope_digest,audience_digest,decisions_digest,member_count,claim_count,"
            "manifest_codec,resolver_version,policy_version,accepted_at_ms,sealed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                root_id,
                sent.seq,
                sent.message_id,
                sent.target,
                proof.audience.wire_envelope_digest,
                proof.audience.digest,
                proof.decisions_digest,
                2,
                1,
                proof.manifest_codec,
                proof.resolver_version,
                proof.policy_version,
                8,
            ),
        )
    with pytest.raises(IdentityConflict, match="unsealed"):
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    assert sealed_cohort_assignments(store, lookups["Alice"]) == ()
    delivery_count = store.session._connection.execute(
        "SELECT COUNT(*) FROM cohort_delivery_receipts"
    ).fetchone()[0]
    assert delivery_count == 0
    assert store.session._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0


def test_reader_rejects_later_corrupt_row_even_for_earlier_valid_seq(tmp_path: Path) -> None:
    comms, store, root_id, _lookups = _root(tmp_path)
    sent = publish_initial(comms, "sender", "Alice", "first")
    bus_path = tmp_path / "comms" / "bus.jsonl"
    with bus_path.open("ab") as output:
        output.write(b'{"seq": true}\n')
    with pytest.raises(RelationViolationError, match="Unreadable bus row.*byte offset"):
        accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
    assert (
        store.session._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
        == 0
    )


def test_existing_bus_cannot_acquire_private_marker(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    default = Comms(root)
    default.registry.register(default.registry.new_thread(name='sender', tags=frozenset(), worktree=str(root)))
    default.registry.register(default.registry.new_thread(name='Alice', tags=frozenset(), worktree=str(root)))
    default.messaging.send("sender", "Alice", "ordinary").message
    gated = Comms(root)
    before = (root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="unmarked bus data"):
        gated.messaging.initialize_private_initial_protocol()
    assert (root / "bus.jsonl").read_bytes() == before


def test_archived_direct_recipient_has_no_eligible_wake(tmp_path: Path) -> None:
    comms, _store, _root_id, _lookups = _root(tmp_path)
    comms.registry.archive("Alice")
    assert comms.registry.status("Alice") == ArchivedThreadStatus()
    with pytest.raises(RelationViolationError, match="visible"):
        publish_initial(comms, "sender", "Alice", "must not wake")
    assert comms.bus.log.latest_sequence() == 0


def test_private_issuer_rejects_untrusted_ancestor_and_collision(tmp_path: Path) -> None:
    open_parent = tmp_path / "untrusted"
    open_parent.mkdir(mode=0o700)
    root = open_parent / "nested"
    root.mkdir()
    open_parent.chmod(0o777)
    try:
        with pytest.raises(RelationViolationError, match="ancestry"):
            Comms(root).messaging.initialize_private_initial_protocol()
        assert not (root / "bus_meta.json").exists()
    finally:
        open_parent.chmod(0o700)

    comms, _store, _root_id, _lookups = _root(tmp_path)
    # A distinct same-tick owner is rejected at registration, before it can
    # acquire another participant's claim identity or appear in an audience.
    with pytest.raises(RelationViolationError, match="creation identities collide"):
        comms.registry.register(
            comms.registry.new_thread(name='duplicate', tags=frozenset({'other'}), worktree=str(tmp_path), created_at=17002.0)
        )
    assert comms.bus.log.latest_sequence() == 0


def test_simultaneous_accepts_one_applied_one_replay(tmp_path: Path) -> None:
    comms, coordinator, root_id, _ = _root(tmp_path)
    sent = publish_initial(comms, "sender", "#team", "all")
    coordinator.close()

    def worker(_: int) -> str:
        store = Coordination(str(tmp_path / "comms" / "coordinator.sqlite"))
        try:
            result = accept_delivery_cohort(comms.bus, root_id, sent.seq, store)
            return type(result).__name__
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(worker, range(2))) == ["AlreadyApplied", "Applied"]


def test_message_taken_by_a_running_turn_is_delivered_once_and_stays_delivered(
    tmp_path: Path,
) -> None:
    comms, coordinator, root_id, lookups = _root(tmp_path)
    message = publish_initial(comms, "sender", "#team", "read this while you work")
    accepted = accept_delivery_cohort(comms.bus, root_id, message.seq, coordinator)
    alice, bob = sorted(accepted.value.assignments, key=lambda item: item.recipient)

    # Pending cannot jump to delivered: only a message already handed to the
    # running turn (paused) can be proved started there.
    with pytest.raises(IdentityConflict):
        coordinator.assignments.transition_preengagement(
            alice.assignment_id, DeliveredAssignment, expected_revision=alice.revision
        )

    paused = coordinator.assignments.transition_preengagement(
        alice.assignment_id, DeferredAssignment, expected_revision=alice.revision
    ).value
    delivered = coordinator.assignments.transition_preengagement(
        alice.assignment_id, DeliveredAssignment, expected_revision=paused.revision
    ).value
    assert type(delivered.lifecycle) is DeliveredAssignment
    assert delivered.lifecycle.mode == alice.lifecycle.mode
    assert delivered.execution_id is None

    # Delivered is final: no wake, no second delivery.
    for after in (DeferredAssignment, type(alice.lifecycle)):
        with pytest.raises(IdentityConflict):
            coordinator.assignments.transition_preengagement(
                alice.assignment_id, after, expected_revision=delivered.revision
            )
    pending = pending_sealed_assignments(coordinator, lookups["Alice"], "Alice")
    assert alice.assignment_id not in {item.assignment_id for item in pending}

    # The stored rules, not only the Python states, refuse to reopen it.
    with pytest.raises(sqlite3.IntegrityError):
        coordinator.session._connection.execute(
            "UPDATE wake_claims SET lifecycle=(SELECT lifecycle FROM wake_claims "
            "WHERE assignment_id=?), revision=revision+1 WHERE assignment_id=?",
            (bob.assignment_id, alice.assignment_id),
        )
    coordinator.session._connection.rollback()

    # A message the turn never took returns to pending and wakes normally.
    bob_paused = coordinator.assignments.transition_preengagement(
        bob.assignment_id, DeferredAssignment, expected_revision=bob.revision
    ).value
    back = coordinator.assignments.transition_preengagement(
        bob.assignment_id, type(bob.lifecycle), expected_revision=bob_paused.revision
    ).value
    assert type(back.lifecycle) is type(bob.lifecycle)
    pending = pending_sealed_assignments(coordinator, lookups["Bob"], "Bob")
    assert bob.assignment_id in {item.assignment_id for item in pending}
