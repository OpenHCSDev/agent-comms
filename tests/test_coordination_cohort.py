"""Original committed raw row → complete atomic N/K acceptance, no live wake."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination import (
    ClaimDisposition,
    MessageAudience,
    PublicationIntent,
    WakeClaim,
    WakeMode,
    canonical_publication_key,
)
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_store import AlreadyApplied, Applied, IdentityConflict, MutationStore
from agent_comms.declarations import (
    Message,
    MessageBus,
    MessageType,
    RelationViolationError,
    Thread,
    ThreadRole,
    ThreadStatus,
)
from agent_comms.exporting import WireExportFormat, WireExportLimit, WireExportScope
from agent_comms.operations import Comms


def _root(tmp_path: Path) -> tuple[Comms, MutationStore, str, dict[str, str]]:
    root = tmp_path / "comms"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    people = {
        "sender": (17001.0, {"team"}),
        "Alice": (17002.0, {"team"}),
        "Bob": (17003.0, {"team"}),
        "Charlie": (17004.0, {"other"}),
    }
    lookups: dict[str, str] = {}
    for name, (created_at, tags) in people.items():
        comms.registry.register(
            Thread(name=name, worktree=str(root), created_at=created_at, tags=frozenset(tags))
        )
        lookups[name] = stable_thread_lookup(created_at)
    coordinator = MutationStore(str(root / "coordinator.sqlite"), clock_ms=lambda: 4723)
    install_private_cohort_schema(coordinator)
    for name in ("Alice", "Bob", "Charlie"):
        coordinator.register_participant(lookups[name], name, name, committed=True)
    root_id = comms.initialize_private_initial_protocol()
    return comms, coordinator, root_id, lookups


def _intent(message: Message, execution_id: str = "execution-1") -> PublicationIntent:
    return PublicationIntent(
        execution_id=execution_id,
        sender=message.sender,
        exact_target=message.target,
        message_type=message.type,
        notice=message.notice,
        timestamp=message.timestamp,
        payload=message.body,
        payload_digest=hashlib.sha256(message.body.encode()).hexdigest(),
        publication_key=canonical_publication_key(execution_id, message.target),
        expected_message_id=message.message_id,
    )


def test_private_initial_opt_in_full_n_observer_and_exact_public_projection(tmp_path: Path) -> None:
    comms, coordinator, root_id, lookups = _root(tmp_path)
    assert len(root_id) == 32
    assert (tmp_path / "comms" / "bus_meta.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(RelationViolationError, match="Legacy append"):
        comms.send_message("sender", "#team", "Old writer blocked")
    message = comms.send_initial_cohort("sender", "#team", "Hello @Alice")
    raw = json.loads((tmp_path / "comms" / "bus.jsonl").read_text())
    assert (tmp_path / "comms" / "bus.jsonl").stat().st_mode & 0o777 == 0o600
    assert set(raw[PRIVATE_WIRE_FIELD]) == {"version", "initial"}
    assert raw["seq"] == message.seq and raw["id"] == message.message_id
    assert all(PRIVATE_WIRE_FIELD not in item.to_wire() for item in comms.bus.full_history())
    assert [item.seq for item in comms.bus.full_history_page(max_bytes=32).messages] == [
        message.seq
    ]
    verified = comms.bus.read_initial_cohort(root_id, message.seq)
    assert [r.recipient_lookup for r in verified.audience.recipients] == sorted(
        (lookups["Alice"], lookups["Bob"])
    )
    assert len(verified.decisions) == 2
    assert [type(decision).__name__ for decision in verified.decisions].count("NoWakeDecision") == 1
    first = accept_initial_cohort(comms.bus, root_id, message.seq, coordinator)
    assert isinstance(first, Applied)
    assert (first.value.member_count, first.value.claim_count) == (2, 1)
    assert first.value.claims[0].recipient_lookup == lookups["Alice"]
    assert first.value.claims[0].wake_mode is WakeMode.FULL
    assert first.value.accepted_at_ms == 4723
    assert len(sealed_cohort_claims(coordinator, lookups["Alice"])) == 1
    assert sealed_cohort_claims(coordinator, lookups["Bob"]) == ()
    delivery_rows = coordinator._connection.execute(
        "SELECT kind,claim_id FROM cohort_delivery_receipts ORDER BY ordinal"
    ).fetchall()
    assert {row["kind"] for row in delivery_rows} == {"selected", "unmentioned_observer"}
    assert sum(row["claim_id"] is None for row in delivery_rows) == 1


def test_initial_sideband_never_enters_export_or_public_page_budget(tmp_path: Path) -> None:
    comms, _coordinator, _root_id, _lookups = _root(tmp_path)
    first = comms.send_initial_cohort("sender", "#team", "first @Alice")
    second = comms.send_initial_cohort("sender", "#team", "second @Bob")
    budget = sum(len(json.dumps(message.to_wire()).encode()) + 1 for message in (first, second))
    page = comms.bus.full_history_page(max_bytes=budget)
    assert [item.seq for item in page.messages] == [first.seq, second.seq]
    output = tmp_path / "public.jsonl"
    comms.export_wire(
        output,
        format=WireExportFormat.JSONL,
        scope=WireExportScope.for_channel("#team"),
        limit=WireExportLimit.full(),
        export_started_at=time.time() + 5,
    )
    assert PRIVATE_WIRE_FIELD not in output.read_text()
    assert [
        record["message"]["text"] for record in map(json.loads, output.read_text().splitlines()[1:])
    ] == ["first @Alice", "second @Bob"]


def test_crash_before_sql_and_lost_ack_replay_after_rename_and_tags(tmp_path: Path) -> None:
    comms, coordinator, root_id, lookups = _root(tmp_path)
    message = comms.send_initial_cohort("sender", "#team", "all must respond")
    # Full bus fsync occurred; coordinator process crashes before BEGIN IMMEDIATE.
    coordinator.close()
    reopened = Comms(tmp_path / "comms")
    reopened.registry.rename("Alice", "Alicia")
    reopened.registry.register(replace(reopened.registry.require("Bob"), tags=frozenset({"other"})))
    store = MutationStore(str(tmp_path / "comms" / "coordinator.sqlite"), clock_ms=lambda: 4788)
    result = accept_initial_cohort(reopened.bus, root_id, message.seq, store)
    assert isinstance(result, Applied)
    assert (result.value.member_count, result.value.claim_count) == (2, 2)
    assert {claim.recipient for claim in result.value.claims} == {"Alice", "Bob"}
    assert {claim.audience for claim in result.value.claims} == {MessageAudience.COLLECTIVE}
    store.transition_preengagement(
        result.value.claims[0].claim_id, ClaimDisposition.DEFERRED, expected_revision=1
    )
    store.close()
    # The SQL COMMIT succeeded, but the client lost its reply. No reappend or
    # restamping is allowed, even after dispositions and registration evolve.
    recovered = MutationStore(str(tmp_path / "comms" / "coordinator.sqlite"), clock_ms=lambda: 9000)
    again = accept_initial_cohort(reopened.bus, root_id, message.seq, recovered)
    assert isinstance(again, AlreadyApplied)
    assert again.value.accepted_at_ms == 4788
    assert any(claim.disposition is ClaimDisposition.DEFERRED for claim in again.value.claims)
    assert [claim.recipient_lookup for claim in again.value.claims] == [
        claim.recipient_lookup for claim in result.value.claims
    ]
    assert {claim.recipient_lookup for claim in again.value.claims} == {
        lookups["Alice"],
        lookups["Bob"],
    }
    assert recovered._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 2


def test_zero_member_zero_claim_and_direct_message(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    empty = comms.send_initial_cohort("sender", "#missing", "No recipient")
    result = accept_initial_cohort(comms.bus, root_id, empty.seq, store)
    assert isinstance(result, Applied)
    assert (result.value.member_count, result.value.claim_count, result.value.claims) == (0, 0, ())
    assert isinstance(accept_initial_cohort(comms.bus, root_id, empty.seq, store), AlreadyApplied)
    direct = comms.send_initial_cohort("sender", "Alice", "Direct")
    single = accept_initial_cohort(comms.bus, root_id, direct.seq, store)
    assert isinstance(single, Applied)
    assert (single.value.member_count, single.value.claim_count) == (1, 1)
    assert single.value.claims[0].recipient_lookup == lookups["Alice"]
    assert single.value.claims[0].audience is MessageAudience.DIRECT
    comms.registry.rename("Alice", "Alicia")
    with pytest.raises(RelationViolationError, match="stable send binding"):
        comms.send_initial_cohort("sender", "Alice", "No alias")
    with pytest.raises(RelationViolationError, match="routable"):
        comms.send_initial_cohort("sender", "#any", "Not a target")


def test_legacy_preexisting_claim_blocks_entire_batch_and_rollback(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "#team", "Hello @Alice")
    legacy = WakeClaim(
        claim_id="singleton-before-cohort",
        recipient="Alice",
        recipient_lookup=lookups["Alice"],
        wire_seq=sent.seq,
        message_id=sent.message_id,
        exact_target=None,
        audience=MessageAudience.MENTIONED,
        wake_mode=WakeMode.FULL,
        triage_verdict=None,
        disposition=ClaimDisposition.FULL_PENDING,
        accepted_at_ms=1,
        updated_at_ms=1,
    )
    store.accept_claim(legacy)
    with pytest.raises(IdentityConflict, match="legacy singleton"):
        accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    assert store._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0
    assert (
        store._connection.execute("SELECT COUNT(*) FROM cohort_delivery_receipts").fetchone()[0]
        == 0
    )
    assert sealed_cohort_claims(store, lookups["Alice"]) == ()


def test_no_wake_observer_cannot_gain_legacy_claim_after_seal(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "#team", "@Alice hello")
    result = accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    assert isinstance(result, Applied)
    observer_claim = replace(
        result.value.claims[0],
        claim_id="legacy-observer",
        recipient="Bob",
        recipient_lookup=lookups["Bob"],
    )
    with pytest.raises(sqlite3.IntegrityError, match="no-wake observer"):
        store.accept_claim(observer_claim)
    assert store._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 1


def test_corrupt_initial_and_wrong_root_rejected_before_sql(tmp_path: Path) -> None:
    comms, store, root_id, _lookups = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "#team", "Hello @Alice")
    with pytest.raises(RelationViolationError, match="root"):
        accept_initial_cohort(comms.bus, "a" * 32, sent.seq, store)
    bus_path = tmp_path / "comms" / "bus.jsonl"
    original = bus_path.read_bytes()
    row = json.loads(original)
    row[PRIVATE_WIRE_FIELD]["initial"]["decisions"][0]["recipient_lookup"] = "imposter"
    bus_path.write_text(json.dumps(row) + "\n")
    with pytest.raises(RelationViolationError, match="initial"):
        accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    assert store._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0
    bus_path.write_bytes(original[:-1])
    with pytest.raises(RelationViolationError, match="Incomplete bus row"):
        accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    bus_path.write_bytes(original)


def test_keyed_response_replay_after_initial_row_and_receipt_backed_page(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "Alice", "one")
    accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    response_bus = MessageBus(
        tmp_path / "comms" / "bus.jsonl", comms.registry, private_response_writes=True
    )
    expected = Message(sender="Alice", target="sender", body="reply", type=MessageType.INFO)
    intent = _intent(expected)
    response = response_bus.publish_keyed_response(intent)
    assert response_bus.publish_keyed_response(intent) == response
    assert [message.seq for message in response_bus.full_history()] == [sent.seq, response.seq]
    assert [claim.wire_seq for claim in sealed_cohort_claims(store, lookups["Alice"])] == [sent.seq]
    with pytest.raises(RelationViolationError, match="No committed initial"):
        response_bus.read_initial_cohort(root_id, response.seq)


def test_all_channel_excludes_sender_and_nonexecutors_and_control_is_not_forgeable(
    tmp_path: Path,
) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    comms.registry.register(
        Thread(
            name="human",
            tags=frozenset(),
            worktree=str(tmp_path),
            role=ThreadRole.USER,
            created_at=17005.0,
        )
    )
    with pytest.raises(RelationViolationError, match="initial issuer"):
        comms.bus.publish_initial_cohort(
            Message(sender="sender", target="#all", body="system", type=MessageType.INFO),
            control="system_control",
        )
    assert comms.bus.latest_sequence() == 0
    sent = comms.send_initial_cohort("sender", "#all", "ordinary")
    result = accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    assert isinstance(result, Applied)
    assert (result.value.member_count, result.value.claim_count) == (3, 3)
    assert {claim.recipient_lookup for claim in result.value.claims} == {
        lookups["Alice"],
        lookups["Bob"],
        lookups["Charlie"],
    }
    assert {claim.wake_mode for claim in result.value.claims} == {WakeMode.BOUNDED_TRIAGE}


def test_wrong_and_partial_db_receipt_never_accepts_one_n_member(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "#team", "Hello @Alice")
    proof = comms.bus.read_initial_cohort(root_id, sent.seq)
    with store._transaction() as db:
        db.execute(
            "INSERT INTO claim_batch_receipts (wire_root_id,wire_seq,message_id,exact_target,"
            "envelope_digest,audience_digest,decisions_digest,member_count,claim_count,"
            "manifest_codec,resolver_version,policy_version,accepted_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    assert sealed_cohort_claims(store, lookups["Alice"]) == ()
    delivery_count = store._connection.execute(
        "SELECT COUNT(*) FROM cohort_delivery_receipts"
    ).fetchone()[0]
    assert delivery_count == 0
    assert store._connection.execute("SELECT COUNT(*) FROM wake_claims").fetchone()[0] == 0


def test_reader_rejects_later_corrupt_row_even_for_earlier_valid_seq(tmp_path: Path) -> None:
    comms, store, root_id, _lookups = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "Alice", "first")
    bus_path = tmp_path / "comms" / "bus.jsonl"
    with bus_path.open("ab") as output:
        output.write(b'{"seq": true}\n')
    with pytest.raises(RelationViolationError, match="Malformed public bus row"):
        accept_initial_cohort(comms.bus, root_id, sent.seq, store)
    assert store._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0] == 0


def test_default_off_and_existing_bus_cannot_acquire_private_marker(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    default = Comms(root)
    with pytest.raises(RelationViolationError, match="disabled"):
        default.initialize_private_initial_protocol()
    default.registry.register(Thread(name="sender", tags=frozenset(), worktree=str(root)))
    default.registry.register(Thread(name="Alice", tags=frozenset(), worktree=str(root)))
    default.send_message("sender", "Alice", "ordinary")
    gated = Comms(root, private_initial_writes=True)
    before = (root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="fresh bus root"):
        gated.initialize_private_initial_protocol()
    assert (root / "bus.jsonl").read_bytes() == before


def test_archived_direct_recipient_has_no_eligible_wake(tmp_path: Path) -> None:
    comms, _store, _root_id, _lookups = _root(tmp_path)
    comms.registry.archive("Alice")
    assert comms.registry.status("Alice") is ThreadStatus.ARCHIVED
    with pytest.raises(RelationViolationError, match="visible executable"):
        comms.send_initial_cohort("sender", "Alice", "must not wake")
    assert comms.bus.latest_sequence() == 0


def test_private_issuer_rejects_untrusted_ancestor_and_collision(tmp_path: Path) -> None:
    open_parent = tmp_path / "untrusted"
    open_parent.mkdir(mode=0o700)
    root = open_parent / "nested"
    root.mkdir()
    open_parent.chmod(0o777)
    try:
        with pytest.raises(RelationViolationError, match="ancestry"):
            Comms(root, private_initial_writes=True).initialize_private_initial_protocol()
        assert not (root / "bus_meta.json").exists()
    finally:
        open_parent.chmod(0o700)

    comms, _store, _root_id, _lookups = _root(tmp_path)
    comms.registry.register(
        Thread(
            name="duplicate", tags=frozenset({"other"}), worktree=str(tmp_path), created_at=17002.0
        )
    )
    with pytest.raises(RelationViolationError, match="creation identities collide"):
        comms.send_initial_cohort("sender", "#team", "collision even outside selected N")
    assert comms.bus.latest_sequence() == 0


def test_simultaneous_accepts_one_applied_one_replay(tmp_path: Path) -> None:
    comms, coordinator, root_id, _ = _root(tmp_path)
    sent = comms.send_initial_cohort("sender", "#team", "all")
    coordinator.close()

    def worker(_: int) -> str:
        store = MutationStore(str(tmp_path / "comms" / "coordinator.sqlite"))
        try:
            result = accept_initial_cohort(comms.bus, root_id, sent.seq, store)
            return type(result).__name__
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(worker, range(2))) == ["AlreadyApplied", "Applied"]
