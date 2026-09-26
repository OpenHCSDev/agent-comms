"""Prelaunch expected-prompt binding: crash/UNKNOWN/owner-change ordering.

Fake native Pi only; real provider acceptance stays with the pinned executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms import proven_source_coverage as coverage_module
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime import run_one_sealed_claim
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import IdentityConflict, MutationStore, StaleFence
from agent_comms.declarations import MessageBus, RelationViolationError, Thread
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import NativePiUnavailable, read_tracked_input_digest
from agent_comms.native_prompt_binding import (
    binding_store_path,
    install_prompt_binding_schema,
    native_request_digest,
    read_expected_prompt_binding,
)
from agent_comms.native_source_cursor import (
    advance_current_native_cursor,
    read_current_native_cursor,
)
from agent_comms.operations import Comms
from agent_comms.proven_source_coverage import read_proven_source_coverage


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("sealed runtime requires a real, disposable /var/tmp root")
    with tempfile.TemporaryDirectory(prefix="ac-binding-test-", dir="/var/tmp") as root:
        yield Path(root)


def _root(tmp_path: Path):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    people = [
        Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()),
        Thread("alpha", frozenset({"team"}), str(tmp_path), pid=os.getpid(), task="math answers"),
    ]
    for person in people:
        comms.register(person)
    root_id = comms.initialize_private_initial_protocol()
    message = comms.send_initial_cohort("sender", "#team", "Compute 17+25.")
    initial = comms.bus.read_initial_cohort(root_id, message.seq)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        for recipient in initial.audience.recipients:
            store.register_participant(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        accepted = accept_initial_cohort(comms.bus, root_id, message.seq, store)
        assert accepted.value.member_count == len(initial.audience.recipients)
    return root, root_id, comms, initial, people


async def test_suppressed_binding_insert_denies_native_send(tmp_path, monkeypatch):
    from agent_comms import native_prompt_binding as binding

    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    original = binding.sidecar_connection

    @contextmanager
    def suppressed(*args, **kwargs):
        with original(*args, **kwargs) as db:
            # Inject after schema admission to independently test insert/readback,
            # not only the complete-schema negative in test_private_sidecar.
            db.execute(
                "CREATE TRIGGER suppress BEFORE INSERT ON prompt_bindings "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
            yield db

    monkeypatch.setattr(binding, "sidecar_connection", suppressed)
    with pytest.raises(IdentityConflict, match="insert did not preserve exact identity"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert calls == []


async def test_uncertain_binding_commit_denies_native_send_and_retry(tmp_path, monkeypatch):
    from agent_comms import private_sidecar

    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    publish = private_sidecar._publish

    def uncertain(*args, **kwargs):
        # Model a lost success receipt after the snapshot really became durable.
        publish(*args, **kwargs)
        raise private_sidecar.SidecarCommitUnknown("lost commit receipt")

    monkeypatch.setattr(private_sidecar, "_publish", uncertain)
    with pytest.raises(private_sidecar.SidecarCommitUnknown):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert calls == []
    # Reservation is already deferred: a new run cannot resend this input even
    # when durable binding bytes happen to be present after an uncertain return.
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
        is None
    )
    assert calls == []


def _fake_model(*, decision: str = "FULL", digest_override: str | None = None):
    calls: list[tuple[str, str]] = []

    async def fake(package, *, input_id, prompt, worktree, session_dir, session_file=None, **_):
        def admitted():
            with _["prompt_send_boundary"]():
                calls.append((input_id, prompt))

        await asyncio.to_thread(admitted)
        if session_file is None:
            session_file = session_dir / f"{input_id}.jsonl"
            entries = [{"type": "session", "id": "isolated-session"}]
            proof_rows = []
        else:
            entries = [json.loads(line) for line in session_file.read_text().splitlines()]
            proof_rows = [
                json.loads(line)
                for line in Path(str(session_file) + ".input-proof").read_text().splitlines()
            ]
        generation = 1 + max((row["requestGeneration"] for row in proof_rows), default=0)
        entry_id = hashlib.sha256(input_id.encode()).hexdigest()[:16]
        # Real pinned native semantics: the tracked digest covers the
        # pi-input-request-v1 request envelope, not bare prompt bytes.
        digest = digest_override or native_request_digest(prompt)
        entries.append(
            {
                "type": "message",
                "id": entry_id,
                "message": {"role": "user", "inputId": input_id, "inputDigest": digest},
            }
        )
        session_file.write_text("".join(json.dumps(row) + "\n" for row in entries))
        session_file.chmod(0o600)
        context_digest = hashlib.sha256((input_id + str(generation)).encode()).hexdigest()
        proof_rows.append(
            {
                "schema": 1,
                "type": "context_committed",
                "sessionId": "isolated-session",
                "inputId": input_id,
                "sessionEntryId": entry_id,
                "requestGeneration": generation,
                "llmContextDigest": context_digest,
            }
        )
        proof_file = Path(str(session_file) + ".input-proof")
        proof_file.write_text("".join(json.dumps(row) + "\n" for row in proof_rows))
        proof_file.chmod(0o600)
        reply = json.dumps({"decision": decision}) if "bounded triage" in prompt else "42"
        from agent_comms.native_pi import NativeContextProof, NativeTurnResult

        return NativeTurnResult(
            reply,
            NativeContextProof(
                input_id, "isolated-session", entry_id, generation, context_digest, session_file
            ),
        )

    return fake, calls


async def test_binding_matches_journal_and_exposes_equality(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", fake)
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    )
    assert turn is not None
    assert turn.cursor_status == "proven"
    assert len(calls) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        current = read_current_native_cursor(
            comms.bus, store, wire_root_id=root_id, owner_name="alpha"
        )
        assert current is not None and current.input_id == turn.input_id
        assert current.injected_seq == initial.message.seq
        assert current.covered_seq == initial.message.seq
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert len(evidence) == 1 and evidence[0].stage == "triage"
        binding = read_expected_prompt_binding(store, evidence[0].input_id)
        assert binding is not None
        assert binding.source_seq == initial.message.seq
        assert binding.message_id == initial.message.message_id
        assert binding.stage == "triage" and binding.claim_id == evidence[0].claim_id
        assert binding.owner_thread == "alpha" and binding.wire_root_id == root_id
        # The binding digest is the pinned NATIVE request digest of the exact
        # prompt bytes sent to Pi (not the bare text hash).
        assert binding.expected_prompt_digest == native_request_digest(calls[0][1])
        session_file = evidence[0].context.session_file
        assert read_tracked_input_digest(session_file, evidence[0].input_id) == (
            binding.expected_prompt_digest
        )
        assert evidence[0].expected_prompt_digest == binding.expected_prompt_digest
        assert evidence[0].expected_prompt_equality_established is True


async def test_source_coverage_stops_at_missing_claim_and_unknown_input(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    second = comms.send_initial_cohort("sender", "#team", "Second source.")
    lookup = stable_thread_lookup(people[1].created_at)
    bus = comms.bus
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        before = read_proven_source_coverage(
            bus, store, wire_root_id=root_id, recipient_lookup=lookup
        )
        assert before.covered_seq == 0 and before.blocked_seq == first.message.seq
        accept_initial_cohort(bus, root_id, second.seq, store)
    first_turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert first_turn is not None and first_turn.cursor_status == "proven"
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        middle = read_proven_source_coverage(
            bus, store, wire_root_id=root_id, recipient_lookup=lookup
        )
        assert middle.covered_seq == first.message.seq
        assert middle.injected_source_seqs == (first.message.seq,)
        assert middle.blocked_seq == second.seq
        current = read_current_native_cursor(bus, store, wire_root_id=root_id, owner_name="alpha")
        assert current is not None and current.injected_seq == first.message.seq
    second_turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert second_turn is not None and second_turn.cursor_status == "proven"
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        after = read_proven_source_coverage(
            bus, store, wire_root_id=root_id, recipient_lookup=lookup
        )
        assert after.covered_seq == second.seq
        assert after.injected_source_seqs == (first.message.seq, second.seq)
        assert after.blocked_seq is None
        current = read_current_native_cursor(bus, store, wire_root_id=root_id, owner_name="alpha")
        assert current is not None and current.injected_seq == second.seq
        assert current.input_id == second_turn.input_id
    assert len(calls) == 2


async def test_source_coverage_mismatch_cannot_skip_to_later_proof(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    bad, _ = _fake_model(decision="IGNORE", digest_override="b" * 64)
    good, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", bad)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    second = comms.send_initial_cohort("sender", "alpha", "Second selected source.")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, second.seq, store)
    monkeypatch.setattr(runtime, "run_native_pi_turn", good)
    later = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert later is not None and later.cursor_status == "blocked_gap"
    assert len(calls) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        coverage = read_proven_source_coverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        )
        assert coverage.covered_seq == 0
        assert coverage.injected_source_seqs == ()
        assert coverage.blocked_seq == first.message.seq
        assert (
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
            is None
        )
        with pytest.raises(IdentityConflict, match="bounded private initial scan"):
            read_proven_source_coverage(
                comms.bus,
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[1].created_at),
                limit=1,
            )


def test_source_coverage_refuses_early_and_caps_bytes_before_bus_guard(tmp_path, monkeypatch):
    root, root_id, comms, _, people = _root(tmp_path)
    for index in range(8):
        comms.send_initial_cohort("sender", "alpha", f"Additional source {index}")
    lookup = stable_thread_lookup(people[1].created_at)
    scanned = 0
    original_rows = MessageBus._verified_private_rows_unlocked

    def observed_rows(self, marker):
        nonlocal scanned
        for row in original_rows(self, marker):
            scanned += 1
            yield row

    monkeypatch.setattr(MessageBus, "_verified_private_rows_unlocked", observed_rows)
    with (
        MutationStore(str(root / "coordination.sqlite3")) as store,
        pytest.raises(IdentityConflict, match="bounded private initial scan"),
    ):
        read_proven_source_coverage(
            comms.bus, store, wire_root_id=root_id, recipient_lookup=lookup, limit=1
        )
    assert scanned == 2  # Not nine eager initial DTOs before rejecting.
    scanned = 0
    monkeypatch.setattr(coverage_module, "_MAX_BUS_ROWS", 1)
    with (
        MutationStore(str(root / "coordination.sqlite3")) as store,
        pytest.raises(IdentityConflict, match="row or scan deadline"),
    ):
        read_proven_source_coverage(comms.bus, store, wire_root_id=root_id, recipient_lookup=lookup)
    assert scanned == 2
    monkeypatch.setattr(coverage_module, "_MAX_BUS_BYTES", 10)
    with (
        MutationStore(str(root / "coordination.sqlite3")) as store,
        pytest.raises(RelationViolationError, match="bounded read budget"),
    ):
        read_proven_source_coverage(comms.bus, store, wire_root_id=root_id, recipient_lookup=lookup)
    assert scanned == 2  # Refused before the private bus row iterator.


async def test_source_coverage_distinguishes_no_wake_from_native_injection(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    beta = Thread("beta", frozenset({"team"}), str(tmp_path), pid=os.getpid())
    comms.register(beta)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        store.register_participant(
            stable_thread_lookup(beta.created_at), "beta", "beta", committed=True
        )
    second = comms.send_initial_cohort("sender", "#team", "@beta please review.")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, second.seq, store)
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
        is not None
    )
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        coverage = read_proven_source_coverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        )
        assert coverage.covered_seq == second.seq
        assert coverage.injected_source_seqs == (first.message.seq,)
        assert coverage.no_wake_seqs == (second.seq,)
        assert coverage.blocked_seq is None
    assert len(calls) == 1


async def test_source_coverage_stops_at_triage_without_required_full(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")

    async def fail_full(package, **kwargs):
        if "bounded triage" in kwargs["prompt"]:
            return await fake(package, **kwargs)
        raise NativePiUnavailable("full launch failed after triage")

    monkeypatch.setattr(runtime, "run_native_pi_turn", fail_full)
    with pytest.raises(NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert len(calls) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        coverage = read_proven_source_coverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        )
        assert coverage.covered_seq == 0
        assert coverage.injected_source_seqs == ()
        assert coverage.blocked_seq == first.message.seq


async def test_current_cursor_never_promotes_old_owner_epoch(tmp_path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert turn is not None and turn.cursor_status == "proven"
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        old = read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
        assert old is not None and old.injected_seq == initial.message.seq
    comms.registry.unregister("alpha")
    comms.registry.heartbeat("alpha")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
            is None
        )
        retained = store._connection.execute(
            "SELECT owner_admission_epoch,input_id FROM native_runtime_source_cursors"
        ).fetchone()
        assert tuple(retained) == (old.owner_admission_epoch, turn.input_id)
    assert len(calls) == 1


async def test_old_input_id_cannot_directly_seed_new_admission_cursor(tmp_path, monkeypatch):
    root, root_id, comms, _, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert turn is not None and turn.cursor_status == "proven"
    comms.registry.unregister("alpha")
    comms.registry.heartbeat("alpha")
    owner = comms.registry.require("alpha")
    epoch = comms.registry.snapshot().admission_generations["alpha"]
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        lookup = stable_thread_lookup(people[1].created_at)
        generation = store.participant(lookup).generation
        assert (
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
            is None
        )
        assert (
            advance_current_native_cursor(
                comms.bus,
                store,
                wire_root_id=root_id,
                owner=owner,
                owner_admission_epoch=epoch,
                owner_generation=generation,
                committed_input_id=turn.input_id,
            )
            is None
        )
        assert (
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
            is None
        )
        old_epoch = store._connection.execute(
            "SELECT sent_owner_admission_epoch FROM native_runtime_inputs WHERE input_id=?",
            (turn.input_id,),
        ).fetchone()[0]
        assert old_epoch != epoch
        with pytest.raises(sqlite3.IntegrityError, match="input identity is frozen"):
            store._connection.execute(
                "UPDATE native_runtime_inputs SET sent_owner_admission_epoch=? WHERE input_id=?",
                (epoch, turn.input_id),
            )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_source_cursors"
            ).fetchone()[0]
            == 1
        )
    assert len(calls) == 1


async def test_current_cursor_rejects_forged_high_water(tmp_path, monkeypatch):
    root, root_id, comms, first, _ = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert turn is not None and turn.cursor_status == "proven"
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        store._connection.execute(
            "UPDATE native_runtime_source_cursors SET covered_seq=?",
            (first.message.seq + 100,),
        )
        with pytest.raises(IdentityConflict, match="exceeds canonical source proof"):
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
    assert len(calls) == 1


async def test_current_cursor_alias_refusal_and_new_owner_generation(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    old_turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert old_turn is not None and old_turn.cursor_status == "proven"
    lookup = stable_thread_lookup(people[1].created_at)
    comms.registry.rename("alpha", "alpha-new")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        store.advance_owner_generation(lookup, "alpha-new", expected_generation=1)
        assert (
            read_current_native_cursor(
                comms.bus, store, wire_root_id=root_id, owner_name="alpha-new"
            )
            is None
        )
    with pytest.raises(RelationViolationError, match="stable send binding"):
        comms.send_initial_cohort("sender", "alpha", "Unbound retained alias.")
    second = comms.send_initial_cohort("sender", "alpha-new", "Canonical recipient.")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, second.seq, store)
    fresh = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha-new", native_package=tmp_path
    )
    assert fresh is not None and fresh.cursor_status == "proven"
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        cursor = read_current_native_cursor(
            comms.bus, store, wire_root_id=root_id, owner_name="alpha-new"
        )
        assert cursor is not None
        assert cursor.owner_generation == 2 and cursor.owner_thread == "alpha-new"
        assert cursor.injected_seq == second.seq and cursor.input_id == fresh.input_id
        assert cursor.covered_seq == second.seq
        rows = store._connection.execute(
            "SELECT owner_generation,input_id FROM native_runtime_source_cursors "
            "ORDER BY owner_generation"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(1, old_turn.input_id), (2, fresh.input_id)]
    assert len(calls) == 2 and first.message.seq < second.seq


async def test_full_stage_binding_joins_after_triage_engagement(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", fake)
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    )
    assert turn is not None
    assert turn.cursor_status == "proven"
    assert len(calls) == 2  # triage probe, then the FULL answer turn
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [row.stage for row in evidence] == ["triage", "full"]
        for row in evidence:
            assert row.expected_prompt_equality_established is True
            binding = read_expected_prompt_binding(store, row.input_id)
            assert binding is not None and binding.stage == row.stage
            assert binding.expected_prompt_digest == row.expected_prompt_digest
        full = evidence[1]
        assert full.execution_id is not None and full.attempt_ordinal == 1
        assert read_expected_prompt_binding(store, full.input_id).execution_id == full.execution_id


async def test_journal_digest_mismatch_is_not_equality(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, _ = _fake_model(digest_override="b" * 64)
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", fake)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        # A mismatched journal cannot become a live-recorded source receipt.
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert evidence == ()
        binding = store._connection.execute(
            "SELECT input_id,session_id FROM native_runtime_inputs WHERE claim_id IN "
            "(SELECT claim_id FROM wake_claims WHERE wire_seq=?)",
            (initial.message.seq,),
        ).fetchone()
        assert binding is not None and binding[1] is None
    # The uncertain, already reserved input remains deferred, not retried.
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
        is None
    )


async def test_full_stage_digest_mismatch_is_unproven_and_never_replayed(tmp_path, monkeypatch):
    root, root_id, _, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")

    async def corrupt_full(package, **kwargs):
        if "bounded triage" in kwargs["prompt"]:
            return await fake(package, **kwargs)
        bad, _ = _fake_model(digest_override="b" * 64)
        return await bad(package, **kwargs)

    monkeypatch.setattr(runtime, "run_native_pi_turn", corrupt_full)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert len(calls) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [row.stage for row in evidence] == ["triage"]
        assert evidence[0].expected_prompt_equality_established
        rows = store._connection.execute(
            "SELECT stage,session_id FROM native_runtime_inputs ORDER BY stage"
        ).fetchall()
        assert [(row["stage"], row["session_id"] is not None) for row in rows] == [
            ("full", False),
            ("triage", True),
        ]
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
        is None
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("stage", "field", "bad"),
    [
        ("triage", "owner_lookup", "f" * 32),
        ("triage", "owner_thread", "attacker"),
        ("triage", "owner_generation", 99),
        ("full", "execution_id", "forged-execution"),
        ("full", "attempt_ordinal", 99),
        ("full", "owner_thread", "attacker"),
    ],
)
async def test_live_binding_rejects_tampered_owner_and_attempt_before_proof(
    tmp_path, monkeypatch, stage, field, bad
):
    root, root_id, _, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    real_read = runtime.read_expected_prompt_binding
    tampered = False

    async def fake_then_tamper(package, **kwargs):
        nonlocal tampered
        result = await fake(package, **kwargs)
        if ("triage" if "bounded triage" in kwargs["prompt"] else "full") == stage:
            tampered = True
        return result

    def changed_binding(store, input_id, **kwargs):
        binding = real_read(store, input_id, **kwargs)
        return replace(binding, **{field: bad}) if tampered and binding is not None else binding

    monkeypatch.setattr(runtime, "run_native_pi_turn", fake_then_tamper)
    monkeypatch.setattr(runtime, "read_expected_prompt_binding", changed_binding)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert tampered and len(calls) == (1 if stage == "triage" else 2)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [row.stage for row in evidence] == ([] if stage == "triage" else ["triage"])
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_inputs "
                "WHERE stage=? AND session_id IS NOT NULL",
                (stage,),
            ).fetchone()[0]
            == 0
        )
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
        is None
    )
    assert len(calls) == (1 if stage == "triage" else 2)


@pytest.mark.parametrize(
    ("stage", "field", "value"),
    [
        ("triage", "owner_thread", "attacker"),
        ("full", "execution_id", "forged-execution"),
        ("full", "attempt_ordinal", 99),
    ],
)
async def test_live_gate_refuses_persisted_sidecar_identity_tamper(
    tmp_path, monkeypatch, stage, field, value
):
    from agent_comms import native_prompt_binding as binding_module

    root, root_id, _, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        path = binding_store_path(store)

    async def mutate_after_admitted_send(package, **kwargs):
        result = await fake(package, **kwargs)
        current_stage = "triage" if "bounded triage" in kwargs["prompt"] else "full"
        if current_stage == stage:
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER prompt_binding_update_guard")
                update = db.execute(
                    f"UPDATE prompt_bindings SET {field}=? WHERE input_id=?",
                    (value, kwargs["input_id"]),
                )
                assert update.rowcount == 1
                db.execute(binding_module._DDL[2][1])
        return result

    monkeypatch.setattr(runtime, "run_native_pi_turn", mutate_after_admitted_send)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert len(calls) == (1 if stage == "triage" else 2)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [row.stage for row in evidence] == ([] if stage == "triage" else ["triage"])
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_inputs WHERE stage=? AND session_id IS NULL",
                (stage,),
            ).fetchone()[0]
            == 1
        )


async def test_owner_change_between_reserve_and_bind_refuses_and_never_launches(
    tmp_path, monkeypatch
):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", fake)

    real_bind = runtime.bind_expected_prompt

    def generation_bumps_then_bind(*args, **kwargs):
        # Simulate a concurrent owner generation advance after the reservation
        # committed but before the binding write.
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            store.advance_owner_generation(
                stable_thread_lookup(people[1].created_at),
                "alpha",
                expected_generation=1,
            )
        return real_bind(*args, **kwargs)

    monkeypatch.setattr(
        "agent_comms.coordinated_runtime.bind_expected_prompt", generation_bumps_then_bind
    )
    with pytest.raises((IdentityConflict, StaleFence)) as error:
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    assert calls == []
    assert "owner" in str(error.value).lower() or "generation" in str(error.value).lower()


async def test_launch_failure_after_binding_leaves_input_unproven(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)

    async def dying(package, **_):
        raise NativePiUnavailable("fake backend died after the prelaunch binding")

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", dying)
    with pytest.raises(NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        # No live proof row exists: the reserved input is invisible to the
        # historical view and its binding can never be promoted on its own.
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert evidence == ()
        # The claim stays deferred (no auto-retry) and unproven.
        from agent_comms.coordination_cohort import sealed_cohort_claims

        claims = sealed_cohort_claims(store, stable_thread_lookup(people[1].created_at))
        assert [claim.disposition.value for claim in claims] == ["deferred"]
        assert read_expected_prompt_binding(store, "0" * 32) is None
    # The sidecar binding row exists and stays immutable, but no equality is
    # reported anywhere because the live proof never arrived.
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        all_bindings = _all_bindings(store)
        assert len(all_bindings) == 1
        assert all_bindings[0].stage == "triage"


def _all_bindings(store):
    import sqlite3

    path = binding_store_path(store)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT input_id FROM prompt_bindings").fetchall()
    return [read_expected_prompt_binding(store, row["input_id"]) for row in rows]


def test_native_request_digest_matches_real_pinned_module():
    """Cross-check our digest against the REAL compiled _claimNativeInput."""
    import shutil
    import subprocess

    module = None
    for candidate in (
        "/dev/shm/pr48-production-native-33YdZB/node_modules/@earendil-works/"
        "pi-coding-agent/dist/core/agent-session.js",
        "/home/ts/.local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent/"
        "dist/core/agent-session.js",
    ):
        if Path(candidate).exists():
            module = candidate
            break
    if module is None or shutil.which("node") is None:
        pytest.skip("real pinned native module or node unavailable")
    script = f"""
import('{module}').then(m => {{
  const fn = m.AgentSession.prototype._claimNativeInput;
  const ctx = {{_nativeProofPath() {{}}, _nativeInputClaims: new Map()}};
  fn.call(ctx, '{'a' * 32}', {{
    kind: 'prompt',
    text: {json.dumps("bound prompt reply exactly")},
    images: null,
    streamingBehavior: null,
    expandPromptTemplates: true,
    source: 'interactive',
  }});
  console.log([...ctx._nativeInputClaims.values()][0]);
}}).catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    real = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert real.returncode == 0, real.stderr
    observed = real.stdout.strip()
    assert len(observed) == 64
    assert observed == native_request_digest("bound prompt reply exactly")
