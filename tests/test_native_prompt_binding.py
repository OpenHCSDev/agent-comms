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
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, StaleFence
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import NativePiUnavailable, read_tracked_input_digest
from agent_comms.native_prompt_binding import (
    binding_store_path,
    install_prompt_binding_schema,
    native_request_digest,
    read_expected_prompt_binding,
)
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession


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
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ),
        Thread(
            "alpha",
            frozenset({"team"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            task="math answers",
            model="openai-codex/gpt-6-sol",
        ),
    ]
    for person in people:
        comms.registry.declare(person)
    root_id = comms.messaging.initialize_private_initial_protocol()
    message = comms.messaging.send_initial_cohort("sender", "#team", "Compute 17+25.")
    initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        for recipient in initial.audience.recipients:
            store.participants.register(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        accepted = accept_delivery_cohort(comms.bus, root_id, message.seq, store)
        assert accepted.value.member_count == len(initial.audience.recipients)
    return root, root_id, comms, initial, people


async def test_suppressed_binding_insert_denies_native_send(tmp_path, monkeypatch):
    from agent_comms import native_prompt_binding as binding

    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    original = binding.sidecar_connection

    @contextmanager
    def suppressed(*args, **kwargs):
        with original(*args, **kwargs) as db:
            # Inject after schema admission to independently test insert/readback,
            # not only the complete-schema negative in test_private_sidecar.
            db.execute(
                "CREATE TRIGGER suppress BEFORE INSERT ON prompt_binding "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
            yield db

    monkeypatch.setattr(binding, "sidecar_connection", suppressed)
    with pytest.raises(IdentityConflict, match="insert did not preserve exact identity"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    assert calls == []


async def test_uncertain_binding_commit_denies_native_send_and_retry(tmp_path, monkeypatch):
    from agent_comms import private_sidecar

    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    publish = private_sidecar._publish

    def uncertain(*args, **kwargs):
        # Model a lost success receipt after the snapshot really became durable.
        publish(*args, **kwargs)
        raise private_sidecar.SidecarCommitUnknown("lost commit receipt")

    monkeypatch.setattr(private_sidecar, "_publish", uncertain)
    with pytest.raises(private_sidecar.SidecarCommitUnknown):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    assert calls == []
    # Reservation is already deferred: a new run cannot resend this input even
    # when durable binding bytes happen to be present after an uncertain return.
    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
        is None
    )
    assert calls == []


def _fake_model(*, decision: str = "FULL", digest_override: str | None = None):
    calls: list[tuple[str, str]] = []

    async def fake(package, *, input_id, prompt, worktree, session_dir, session_file=None, **_):
        fresh = session_file is None
        if fresh:
            session_file = session_dir / f"{input_id}.jsonl"
            session_file.write_text(
                json.dumps({"type": "session", "id": "isolated-session"}) + "\n"
            )
            session_file.chmod(0o600)
        assert session_file is not None

        def admitted():
            with _["prompt_send_boundary"](session_file):
                calls.append((input_id, prompt))

        await asyncio.to_thread(admitted)
        if fresh:
            entries = [{"type": "session", "id": "isolated-session"}]
            proof_rows = []
        else:
            entries = [json.loads(line) for line in session_file.read_text().splitlines()]
            proof_rows = read_proof_rows(session_file)
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
        write_proof_rows(session_file, proof_rows)
        reply = json.dumps({"decision": decision}) if "bounded triage" in prompt else "42"
        from agent_comms.native_pi import NativeContextProof, NativeTurnResult

        observer = _.get("observe_event")
        if observer is not None:
            from agent_comms.pi_events import PiEvent

            await observer(
                PiEvent.from_wire(
                    {
                        "type": "response",
                        "id": "native-prompt",
                        "command": "prompt",
                        "success": True,
                    }
                )
            )
            await observer(PiEvent.from_wire({"type": "context_committed", "inputId": input_id}))
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
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)
    turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    ).run()
    assert turn is not None
    assert turn.cursor_status == "proven"
    assert len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        current = NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(
            owner_name="alpha"
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
        assert len(evidence) == 1 and isinstance(evidence[0].execution, TriageNativeExecution)
        binding = read_expected_prompt_binding(store, evidence[0].input_id)
        assert binding is not None
        assert binding.stage is TriageNativeExecution
        assert evidence[0].source_seq == initial.message.seq
        assert evidence[0].source_message_id == initial.message.message_id
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
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    second = comms.messaging.send_initial_cohort("sender", "#team", "Second source.")
    lookup = stable_thread_lookup(people[1].created_at)
    bus = comms.bus
    with Coordination(str(root / "coordination.sqlite3")) as store:
        before = SourceCoverage(bus, store, wire_root_id=root_id, recipient_lookup=lookup).read()
        assert before.covered_seq == 0 and before.blocked_seq == first.message.seq
        accept_delivery_cohort(bus, root_id, second.seq, store)
    first_turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert first_turn is not None and first_turn.cursor_status == "proven"
    with Coordination(str(root / "coordination.sqlite3")) as store:
        middle = SourceCoverage(bus, store, wire_root_id=root_id, recipient_lookup=lookup).read()
        assert middle.covered_seq == first.message.seq
        assert middle.injected_source_seqs == (first.message.seq,)
        assert middle.blocked_seq == second.seq
        current = NativeSourceCursor(bus, store, wire_root_id=root_id).read(owner_name="alpha")
        assert current is not None and current.injected_seq == first.message.seq
    second_turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert second_turn is not None and second_turn.cursor_status == "proven"
    with Coordination(str(root / "coordination.sqlite3")) as store:
        after = SourceCoverage(bus, store, wire_root_id=root_id, recipient_lookup=lookup).read()
        assert after.covered_seq == second.seq
        assert after.injected_source_seqs == (first.message.seq, second.seq)
        assert after.blocked_seq is None
        current = NativeSourceCursor(bus, store, wire_root_id=root_id).read(owner_name="alpha")
        assert current is not None and current.injected_seq == second.seq
        assert current.input_id == second_turn.input_id
    assert len(calls) == 2


async def test_source_coverage_mismatch_cannot_skip_to_later_proof(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    bad, _ = _fake_model(decision="IGNORE", digest_override="b" * 64)
    good, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", bad)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    second = comms.messaging.send_initial_cohort("sender", "alpha", "Second selected source.")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_delivery_cohort(comms.bus, root_id, second.seq, store)
    monkeypatch.setattr(TrackedTurnSession, "execute", good)
    later = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert later is not None and later.cursor_status == "blocked_gap"
    assert len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        coverage = SourceCoverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        ).read()
        assert coverage.covered_seq == 0
        assert coverage.injected_source_seqs == ()
        assert coverage.blocked_seq == first.message.seq
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )
        with pytest.raises(IdentityConflict, match="bounded private initial scan"):
            SourceCoverage(
                comms.bus,
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[1].created_at),
            ).read(limit=1)


async def test_source_coverage_distinguishes_no_wake_from_native_injection(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    beta = Thread(
        "beta",
        frozenset({"team"}),
        str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    )
    comms.registry.declare(beta)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(
            stable_thread_lookup(beta.created_at), "beta", "beta", committed=True
        )
    second = comms.messaging.send_initial_cohort("sender", "#team", "@beta please review.")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_delivery_cohort(comms.bus, root_id, second.seq, store)
    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
        is not None
    )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        coverage = SourceCoverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        ).read()
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

    monkeypatch.setattr(TrackedTurnSession, "execute", fail_full)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    assert len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        coverage = SourceCoverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        ).read()
        assert coverage.covered_seq == 0
        assert coverage.injected_source_seqs == ()
        assert coverage.blocked_seq == first.message.seq


async def test_current_cursor_never_promotes_old_owner_generation(tmp_path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert turn is not None and turn.cursor_status == "proven"
    with Coordination(str(root / "coordination.sqlite3")) as store:
        old = NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
        assert old is not None and old.injected_seq == initial.message.seq
    comms.registry.unregister("alpha")
    comms.registry.heartbeat("alpha")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )
        retained = store.session._connection.execute(
            "SELECT owner_admission_generation,input_id FROM current_native_cursor"
        ).fetchone()
        assert tuple(retained) == (old.owner_admission_generation, turn.input_id)
    assert len(calls) == 1


async def test_old_input_id_cannot_directly_seed_new_admission_cursor(tmp_path, monkeypatch):
    root, root_id, comms, _, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert turn is not None and turn.cursor_status == "proven"
    comms.registry.unregister("alpha")
    comms.registry.heartbeat("alpha")
    owner = comms.registry.require("alpha")
    admission_generation = comms.registry.snapshot().admission_generations["alpha"]
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookup = stable_thread_lookup(people[1].created_at)
        generation = store.participants.get(lookup).participant_generation
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).advance(
                owner=owner,
                owner_admission_generation=admission_generation,
                owner_generation=generation,
                committed_input_id=turn.input_id,
            )
            is None
        )
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )
        old_generation = store.session._connection.execute(
            "SELECT sent_owner_admission_generation FROM native_runtime_input WHERE input_id=?",
            (turn.input_id,),
        ).fetchone()[0]
        assert old_generation != admission_generation
        with pytest.raises(sqlite3.IntegrityError, match="input identity is frozen"):
            store.session._connection.execute(
                "UPDATE native_runtime_input SET sent_owner_admission_generation=? "
                "WHERE input_id=?",
                (admission_generation, turn.input_id),
            )
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM current_native_cursor"
            ).fetchone()[0]
            == 1
        )
    assert len(calls) == 1


async def test_current_cursor_rejects_forged_high_water(tmp_path, monkeypatch):
    root, root_id, comms, first, _ = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert turn is not None and turn.cursor_status == "proven"
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.session._connection.execute(
            "UPDATE current_native_cursor SET covered_seq=?",
            (first.message.seq + 100,),
        )
        with pytest.raises(IdentityConflict, match="exceeds canonical source proof"):
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
    assert len(calls) == 1


async def test_current_cursor_new_owner_generation_cannot_borrow_proof(tmp_path, monkeypatch):
    root, root_id, comms, first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    old_turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert old_turn is not None and old_turn.cursor_status == "proven"
    lookup = stable_thread_lookup(people[1].created_at)
    comms.registry.rename("alpha", "alpha-new")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.advance_generation(lookup, "alpha-new", expected_generation=1)
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha-new")
            is None
        )
    second = comms.messaging.send_initial_cohort("sender", "alpha-new", "Canonical recipient.")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_delivery_cohort(comms.bus, root_id, second.seq, store)
    fresh = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha-new", native_package=tmp_path
    ).run()
    assert fresh is not None and fresh.cursor_status == "blocked_gap"
    with Coordination(str(root / "coordination.sqlite3")) as store:
        # The old selected native proof remains historical evidence, not a
        # prefix bridge into the renamed owner's new generation/epoch.
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha-new")
            is None
        )
        rows = store.session._connection.execute(
            "SELECT owner_generation,input_id FROM current_native_cursor ORDER BY owner_generation"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(1, old_turn.input_id)]
    assert len(calls) == 2 and first.message.seq < second.seq


async def test_full_stage_binding_joins_after_triage_engagement(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)
    turn = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    ).run()
    assert turn is not None
    assert turn.cursor_status == "proven"
    assert len(calls) == 2  # triage probe, then the FULL answer turn
    with Coordination(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [type(row.execution) for row in evidence] == [TriageNativeExecution, FullNativeExecution]
        for row in evidence:
            assert row.expected_prompt_equality_established is True
            binding = read_expected_prompt_binding(store, row.input_id)
            assert binding is not None and binding.execution == row.execution
            assert binding.expected_prompt_digest == row.expected_prompt_digest
        full = evidence[1]
        assert full.execution.require_attempt().attempt_ordinal == 1
        assert read_expected_prompt_binding(store, full.input_id).execution == full.execution


async def test_journal_digest_mismatch_is_not_equality(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, _ = _fake_model(digest_override="b" * 64)
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        # A mismatched journal cannot become a live-recorded source receipt.
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert evidence == ()
        binding = store.session._connection.execute(
            f"SELECT n.input_id,n.session_id FROM native_runtime_input n "
            f"JOIN ({NativeRuntimeInput.source_membership_sql()}) m ON m.input_id=n.input_id "
            "JOIN wake_claims c ON c.assignment_id=m.assignment_id WHERE c.wire_seq=?",
            (initial.message.seq,),
        ).fetchone()
        assert binding is not None and binding[1] is None
    # The uncertain, already reserved input remains deferred, not retried.
    assert (
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
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

    monkeypatch.setattr(TrackedTurnSession, "execute", corrupt_full)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    assert len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [type(row.execution) for row in evidence] == [TriageNativeExecution]
        assert evidence[0].expected_prompt_equality_established
        rows = store.session._connection.execute(
            "SELECT stage,session_id FROM native_runtime_input ORDER BY stage"
        ).fetchall()
        assert [(row["stage"], row["session_id"] is not None) for row in rows] == [
            ("full", False),
            ("triage", True),
        ]
    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
        is None
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("stage", "field", "value"),
    [
        ("triage", "owner_lookup", "f" * 32),
        ("triage", "owner_thread", "attacker"),
        ("triage", "owner_generation", 99),
        ("full", "execution_id", "forged-execution"),
        ("full", "attempt_ordinal", 99),
        ("full", "owner_thread", "attacker"),
    ],
)
async def test_live_gate_refuses_persisted_sidecar_identity_tamper(
    tmp_path, monkeypatch, stage, field, value
):
    from agent_comms import native_prompt_binding as binding_module

    root, root_id, _, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        path = binding_store_path(store)

    async def mutate_after_admitted_send(package, **kwargs):
        result = await fake(package, **kwargs)
        current_stage = "triage" if "bounded triage" in kwargs["prompt"] else "full"
        if current_stage == stage:
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER prompt_binding_update_guard")
                update = db.execute(
                    f"UPDATE prompt_binding SET {field}=? WHERE input_id=?",
                    (value, kwargs["input_id"]),
                )
                assert update.rowcount == 1
                db.execute(binding_module.PromptBinding.triggers()["prompt_binding_update_guard"])
        return result

    monkeypatch.setattr(TrackedTurnSession, "execute", mutate_after_admitted_send)
    with pytest.raises(
        IdentityConflict, match="native send differs from its durable prompt binding"
    ):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    assert len(calls) == (1 if stage == "triage" else 2)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert [type(row.execution) for row in evidence] == ([] if stage == "triage" else [TriageNativeExecution])
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_input WHERE stage=? AND session_id IS NULL",
                (stage,),
            ).fetchone()[0]
            == 1
        )

    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
        is None
    )
    assert len(calls) == (1 if stage == "triage" else 2)


async def test_owner_change_between_reserve_and_bind_refuses_and_never_launches(
    tmp_path, monkeypatch
):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)

    from agent_comms import private_send_admission

    real_bind = private_send_admission.bind_expected_prompt

    def generation_bumps_then_bind(*args, **kwargs):
        # Simulate a concurrent owner generation advance after the reservation
        # committed but before the binding write.
        with Coordination(str(root / "coordination.sqlite3")) as store:
            store.participants.advance_generation(
                stable_thread_lookup(people[1].created_at),
                "alpha",
                expected_generation=1,
            )
        return real_bind(*args, **kwargs)

    monkeypatch.setattr(
        "agent_comms.private_send_admission.bind_expected_prompt", generation_bumps_then_bind
    )
    with pytest.raises((IdentityConflict, StaleFence)) as error:
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    assert calls == []
    assert "owner" in str(error.value).lower() or "generation" in str(error.value).lower()


async def test_launch_failure_after_binding_leaves_input_unproven(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)

    async def dying(package, **_):
        raise NativePiUnavailable("fake backend died after the prelaunch binding")

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", dying)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    with Coordination(str(root / "coordination.sqlite3")) as store:
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
        from agent_comms.coordination_cohort import sealed_cohort_assignments

        claims = sealed_cohort_assignments(store, stable_thread_lookup(people[1].created_at))
        assert [assignment.lifecycle.declared_name for assignment in claims] == ["deferred"]
        assert read_expected_prompt_binding(store, "0" * 32) is None
    # The sidecar binding row exists and stays immutable, but no equality is
    # reported anywhere because the live proof never arrived.
    with Coordination(str(root / "coordination.sqlite3")) as store:
        all_bindings = _all_bindings(store)
        assert len(all_bindings) == 1
        assert all_bindings[0].stage is TriageNativeExecution


def _all_bindings(store):
    import sqlite3

    path = binding_store_path(store)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT input_id FROM prompt_binding").fetchall()
    return [read_expected_prompt_binding(store, row["input_id"]) for row in rows]
