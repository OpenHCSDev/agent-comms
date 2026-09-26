"""Prelaunch expected-prompt binding: crash/UNKNOWN/owner-change ordering.

Fake native Pi only; real provider acceptance stays with the pinned executor.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime import run_one_sealed_claim
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import IdentityConflict, MutationStore, StaleFence
from agent_comms.declarations import Thread
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import NativePiUnavailable, read_tracked_input_digest
from agent_comms.native_prompt_binding import (
    binding_store_path,
    install_prompt_binding_schema,
    native_request_digest,
    read_expected_prompt_binding,
)
from agent_comms.operations import Comms


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


def _fake_model(*, decision: str = "FULL", digest_override: str | None = None):
    calls: list[tuple[str, str]] = []

    async def fake(package, *, input_id, prompt, worktree, session_dir, session_file=None, **_):
        with _["prompt_send_boundary"]():
            calls.append((input_id, prompt))
        if session_file is None:
            session_file = session_dir / "one.jsonl"
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
    assert len(calls) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
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


async def test_full_stage_binding_joins_after_triage_engagement(tmp_path: Path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", fake)
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    )
    assert turn is not None
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
    turn = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    )
    assert turn is not None
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert evidence[0].expected_prompt_digest is not None
        # A journal digest that differs from the binding is not equality.
        assert evidence[0].expected_prompt_equality_established is False


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
