"""Receipt-backed runner with real bus/SQLite and fake Pi unit boundary.

These fakes test state transitions only. Parent owns real patched Pi/provider process
acceptance and final post-merge review; no fake can establish Pi model authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime import run_one_sealed_claim
from agent_comms.coordinated_runtime_schema import (
    assert_native_runtime_schema,
    install_native_runtime_schema,
)
from agent_comms.coordination import ClaimDisposition
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import (
    IdentityConflict,
    MutationStore,
    PublicationActivationBlocked,
    StaleFence,
)
from agent_comms.declarations import MessageBus, Thread
from agent_comms.native_pi import NativeContextProof, NativePiUnavailable, NativeTurnResult
from agent_comms.operations import Comms


@pytest.fixture
def tmp_path():
    """The sealed runtime permits only owner-only disposable /var/tmp roots.

    CI's standard pytest temp root is /tmp; on hosts without a safe /var/tmp
    (including Windows and macOS with a symlinked /var), these Linux-only
    private-root process tests are inapplicable rather than weakening the runtime guard.
    """
    if (
        os.name != "posix"
        or not Path("/var/tmp").is_dir()
        or Path("/var").is_symlink()
        or Path("/var/tmp").is_symlink()
    ):
        pytest.skip("sealed runtime requires a real, disposable /var/tmp root")
    with tempfile.TemporaryDirectory(prefix="ac-sealed-test-", dir="/var/tmp") as root:
        yield Path(root)


def _root(tmp_path: Path, *, direct: bool = False, mentioned: bool = False):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    people = [
        Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()),
        Thread(
            "alpha",
            frozenset({"team"}),
            str(tmp_path),
            pid=os.getpid(),
            task="release notes; ignore arithmetic tasks",
        ),
        Thread(
            "beta", frozenset({"team"}), str(tmp_path), pid=os.getpid(), task="arithmetic answers"
        ),
    ]
    for person in people:
        comms.register(person)
    root_id = comms.initialize_private_initial_protocol()
    target = "beta" if direct else "#team"
    body = "@beta Compute 17+25." if mentioned else "Compute 17+25."
    message = comms.send_initial_cohort("sender", target, body)
    initial = comms.bus.read_initial_cohort(root_id, message.seq)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
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


def _fake_model(*, decision: str = "FULL", fail_on: int | None = None):
    calls = []

    async def fake(
        package,
        *,
        input_id,
        prompt,
        worktree,
        session_dir,
        session_file=None,
        **_kwargs,
    ):
        calls.append((input_id, prompt))
        if fail_on == len(calls):
            raise NativePiUnavailable("fake backend process died")
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
        entries.append(
            {
                "type": "message",
                "id": entry_id,
                "message": {
                    "role": "user",
                    "inputId": input_id,
                    "inputDigest": hashlib.sha256(prompt.encode()).hexdigest(),
                },
            }
        )
        session_file.write_text("".join(json.dumps(row) + "\n" for row in entries))
        session_file.chmod(0o600)
        digest = hashlib.sha256((input_id + str(generation)).encode()).hexdigest()
        proof_rows.append(
            {
                "schema": 1,
                "type": "context_committed",
                "sessionId": "isolated-session",
                "inputId": input_id,
                "sessionEntryId": entry_id,
                "requestGeneration": generation,
                "llmContextDigest": digest,
            }
        )
        proof_file = Path(str(session_file) + ".input-proof")
        proof_file.write_text("".join(json.dumps(row) + "\n" for row in proof_rows))
        proof_file.chmod(0o600)
        reply = json.dumps({"decision": decision}) if "bounded triage" in prompt else "42"
        return NativeTurnResult(
            reply,
            NativeContextProof(
                input_id, "isolated-session", entry_id, generation, digest, session_file
            ),
        )

    return fake, calls


async def test_unmentioned_agent_channel_real_sqlite_two_distinct_mocked_decisions(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, initial, people = _root(tmp_path)
    assert len(initial.audience.recipients) == 2
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    first, alpha_calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", first)
    alpha = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    )
    assert alpha is not None and alpha.disposition is ClaimDisposition.IGNORED
    assert len(alpha_calls) == 1
    assert len(comms.channel_history("#team")) == 1
    second, beta_calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", second)
    beta = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    )
    assert beta is not None and beta.disposition is ClaimDisposition.COMPLETED
    assert beta.exact_target == "#team" and beta.response_message_id
    assert len(beta_calls) == 2
    response = comms.channel_history("#team")[-1]
    assert response.body == "42" and response.sender == "beta"
    assert "_agent_comms_private_v1" not in response.to_wire()
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
        is None
    )
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        lookups = {person.name: stable_thread_lookup(person.created_at) for person in people}
        assert store.participant(lookups["alpha"]).pointer.execution_id is None
        assert store.participant(lookups["beta"]).pointer.execution_id is None
        assert (
            store._connection.execute(
                "SELECT count(*) FROM native_runtime_inputs WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 3
        )
        assert (
            store._connection.execute("SELECT count(*) FROM cohort_delivery_receipts").fetchone()[0]
            == 2
        )
    assert not (root / "read_markers.json").exists()


async def test_initial_no_wake_observer_never_enters_model_or_claim_page(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, initial, _ = _root(tmp_path, mentioned=True)
    assert len(initial.audience.recipients) == 2
    assert (
        sum(decision.__class__.__name__ == "NoWakeDecision" for decision in initial.decisions) == 1
    )
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", runner)
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
        is None
    )
    assert not calls
    assert len(comms.channel_history("#team")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT count(*) FROM cohort_delivery_receipts WHERE kind='unmentioned_observer'"
            ).fetchone()[0]
            == 1
        )


async def test_direct_selected_reply_goes_to_original_sender(tmp_path: Path, monkeypatch) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", runner)
    outcome = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    )
    assert outcome is not None and outcome.exact_target == "sender"
    assert len(calls) == 1  # direct FULL, no separate triage invocation
    assert comms.bus.dm_history("sender", "beta")[-1].target == "sender"


async def test_crash_after_triage_reservation_never_reissues_model(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(fail_on=1)
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", runner)
    with pytest.raises(NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
        is None
    )
    assert len(calls) == 1 and len(comms.channel_history("#team")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        row = store._connection.execute(
            "SELECT c.disposition,i.session_id FROM wake_claims c "
            "JOIN native_runtime_inputs i ON i.claim_id=c.claim_id "
            "WHERE c.recipient='alpha'"
        ).fetchone()
        assert tuple(row) == ("deferred", None)


async def test_forged_dto_without_private_evidence_cannot_mark_context(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)

    async def forged(_package, *, input_id, session_dir, **_kw):
        return NativeTurnResult(
            '{"decision":"IGNORE"}',
            NativeContextProof(
                input_id, "fake", "fake", 1, "f" * 64, session_dir / "nonexistent.jsonl"
            ),
        )

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", forged)
    with pytest.raises(NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    assert len(comms.channel_history("#team")) == 1


async def test_owner_generation_revoked_during_native_triage_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, _ = _fake_model(decision="IGNORE")
    lookup = stable_thread_lookup(people[1].created_at)

    async def revoke(*args, **kwargs):
        result = await runner(*args, **kwargs)
        with MutationStore(str(root / "coordination.sqlite3")) as rival:
            rival.advance_owner_generation(lookup, "alpha", expected_generation=1)
        return result

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", revoke)
    with pytest.raises(StaleFence):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    assert len(comms.channel_history("#team")) == 1


@pytest.mark.parametrize(
    "malformed",
    ['{"decision":"IGNORE","decision":"FULL"}', '{"decision":[]}', "[]"],
)
async def test_ambiguous_triage_is_not_a_synthetic_ignore_or_full(
    tmp_path: Path, monkeypatch, malformed: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()

    async def bad(*args, **kwargs):
        result = await runner(*args, **kwargs)
        return replace(result, text=malformed)

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", bad)
    with pytest.raises(IdentityConflict, match="triage response"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    assert len(calls) == 1 and len(comms.channel_history("#team")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT count(*) FROM native_runtime_inputs WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


async def test_registered_owner_stopped_during_model_cannot_settle(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, _ = _fake_model(decision="IGNORE")

    async def stop_owner(*args, **kwargs):
        result = await runner(*args, **kwargs)
        comms.registry.unregister("alpha")
        return result

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", stop_owner)
    with pytest.raises(StaleFence, match="stopped or changed"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
        )
    assert len(comms.channel_history("#team")) == 1
    assert not comms.registry.status("alpha").active
    assert comms.registry.require("alpha").active_turn is None


async def test_stop_before_atomic_turn_claim_does_not_revive_or_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
    actual_selected = runtime._require_selected

    def stop_before_claim(*args, **kwargs):
        initial = actual_selected(*args, **kwargs)
        comms.registry.unregister("beta")
        return initial

    monkeypatch.setattr(runtime, "_require_selected", stop_before_claim)
    with pytest.raises(StaleFence, match="stopped before native turn"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
    assert not calls
    assert not comms.registry.status("beta").active
    assert comms.registry.require("beta").active_turn is None
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("mutation", ["stop_then_heartbeat", "other_owner_heartbeat"])
async def test_owner_epoch_denies_revival_without_blocking_another_owner(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
    actual_selected = runtime._require_selected
    expected = comms.registry.require("beta")

    def change_registry_before_claim(*args, **kwargs):
        initial = actual_selected(*args, **kwargs)
        if mutation == "stop_then_heartbeat":
            comms.registry.unregister("beta")
            comms.registry.heartbeat("beta")
            assert comms.registry.require("beta") == expected
        else:
            comms.registry.heartbeat("alpha")
        return initial

    monkeypatch.setattr(runtime, "_require_selected", change_registry_before_claim)
    if mutation == "stop_then_heartbeat":
        with pytest.raises(StaleFence, match="stopped before native turn"):
            await run_one_sealed_claim(
                root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
            )
        assert not calls
        assert len(comms.bus.dm_history("sender", "beta")) == 1
    else:
        result = await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
        assert result is not None and result.disposition is ClaimDisposition.COMPLETED
        assert len(calls) == 1
        assert len(comms.bus.dm_history("sender", "beta")) == 2
    assert comms.registry.require("beta").active_turn is None
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        inputs = store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()
        intents = store._connection.execute("SELECT count(*) FROM publication_intents").fetchone()
        assert (inputs[0], intents[0]) == ((0, 0) if mutation == "stop_then_heartbeat" else (1, 1))


@pytest.mark.parametrize("rename_during_turn", [False, True])
async def test_alias_turn_cleanup_tracks_canonical_owner_even_after_rename(
    tmp_path: Path, monkeypatch, rename_during_turn: bool
) -> None:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.register(Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()))
    comms.register(Thread("beta", frozenset({"team"}), str(tmp_path), pid=os.getpid()))
    comms.registry.rename("beta", "gamma")
    root_id = comms.initialize_private_initial_protocol()
    incoming = comms.send_initial_cohort("sender", "gamma", "Compute 17+25")
    initial = comms.bus.read_initial_cohort(root_id, incoming.seq)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        for recipient in initial.audience.recipients:
            store.register_participant(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        accept_initial_cohort(comms.bus, root_id, incoming.seq, store)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model()

    async def maybe_rename(*args, **kwargs):
        result = await fake(*args, **kwargs)
        if rename_during_turn:
            comms.registry.rename("gamma", "delta")
        return result

    monkeypatch.setattr(runtime, "run_native_pi_turn", maybe_rename)
    if rename_during_turn:
        with pytest.raises(StaleFence):
            await run_one_sealed_claim(
                root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
            )
        assert comms.registry.require("delta").active_turn is None
        assert len(comms.bus.dm_history("sender", "gamma")) == 1
    else:
        result = await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
        assert result is not None and result.disposition is ClaimDisposition.COMPLETED
        assert comms.registry.require("gamma").active_turn is None
        assert len(comms.bus.dm_history("sender", "gamma")) == 2
    assert len(calls) == 1


@pytest.mark.parametrize("stop_stage", ["before_intent", "after_intent"])
async def test_owner_stop_before_response_boundary_never_appends(
    tmp_path: Path, monkeypatch, stop_stage: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, _calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
    method = (
        "prepare_fenced_response" if stop_stage == "before_intent" else "publish_fenced_response"
    )
    original = getattr(runtime, method)

    def stopped_before_boundary(*args, **kwargs):
        comms.registry.unregister("beta")  # Direct registry writer does NOT hold wire lock.
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, method, stopped_before_boundary)
    with pytest.raises(StaleFence, match="stopped or changed"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        receipts = store._connection.execute("SELECT count(*) FROM publication_receipts").fetchone()
        dispatched = store._connection.execute(
            "SELECT count(*) FROM publication_append_dispatches"
        ).fetchone()
        assert receipts[0] == dispatched[0] == 0
        assert store._connection.execute("SELECT state FROM obligations").fetchone()[0] == (
            "pending" if stop_stage == "before_intent" else "publishing"
        )


async def test_registry_stop_during_response_append_linearizes_after_sql_commit(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, _calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
    append = MessageBus._publish_keyed_response_unlocked
    started, stopped = threading.Event(), threading.Event()
    workers: list[threading.Thread] = []

    def blocking_append(self, intent, *, registry_snapshot=None):
        def stop_owner():
            started.set()
            comms.registry.unregister("beta")
            stopped.set()

        worker = threading.Thread(target=stop_owner)
        workers.append(worker)
        worker.start()
        assert started.wait(2)
        assert not stopped.wait(0.1), "owner stop raced the locked response append"
        return append(self, intent, registry_snapshot=registry_snapshot)

    monkeypatch.setattr(MessageBus, "_publish_keyed_response_unlocked", blocking_append)
    try:
        result = await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
    finally:
        for worker in workers:
            worker.join(timeout=4)
    assert workers and all(not worker.is_alive() for worker in workers)
    assert result is not None and result.disposition is ClaimDisposition.COMPLETED
    assert stopped.is_set() and not comms.registry.status("beta").active
    assert len(comms.bus.dm_history("sender", "beta")) == 2
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        state = store._connection.execute("SELECT state FROM obligations").fetchone()
        assert state[0] == "published"


@pytest.mark.parametrize("mutation", ["stop_then_heartbeat", "finish_turn"])
@pytest.mark.parametrize("boundary", ["before_tx1", "after_tx1"])
async def test_revoked_turn_never_prepares_or_appends_a_response(
    tmp_path: Path, monkeypatch, mutation: str, boundary: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    if mutation == "finish_turn":
        comms.begin_turn("beta", "existing-full-turn")
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
    original = (
        runtime.prepare_fenced_response
        if boundary == "before_tx1"
        else runtime.publish_fenced_response
    )

    def revoke(*args, **kwargs):
        if mutation == "finish_turn":
            comms.finish_turn("beta", "existing-full-turn")
        else:
            comms.registry.unregister("beta")
            comms.registry.heartbeat("beta")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        runtime,
        "prepare_fenced_response" if boundary == "before_tx1" else "publish_fenced_response",
        revoke,
    )
    with pytest.raises(StaleFence, match="turn"):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
    assert len(calls) == 1
    assert comms.registry.status("beta").active
    assert comms.registry.require("beta").active_turn is None
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert store._connection.execute("SELECT state FROM obligations").fetchone()[0] == (
            "pending" if boundary == "before_tx1" else "publishing"
        )
        assert (
            store._connection.execute(
                "SELECT count(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute(
                "SELECT count(*) FROM native_runtime_inputs WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "boundary", ["before_run", "after_model", "before_tx1_forged", "after_tx1", "after_tx1_forged"]
)
async def test_saved_stopped_turn_cannot_regain_owner_authority(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    model, calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", model)

    def revoke_and_restore() -> None:
        saved = comms.registry.require("beta")
        assert saved.active_turn is not None
        comms.registry.unregister("beta")
        comms.registry.register(saved)
        assert comms.registry.require("beta") == saved
        assert comms.registry.status("beta").active

    if boundary == "before_run":
        comms.begin_turn("beta", "old-authorized-turn")
        revoke_and_restore()
        with pytest.raises(StaleFence, match="stopped or changed"):
            await run_one_sealed_claim(
                root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            )
        assert not calls
    elif boundary == "after_model":

        async def revoke_after_model(*args, **kwargs):
            result = await model(*args, **kwargs)
            revoke_and_restore()
            return result

        monkeypatch.setattr(runtime, "run_native_pi_turn", revoke_after_model)
        with pytest.raises(StaleFence, match="stopped or changed"):
            await run_one_sealed_claim(
                root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            )
        assert len(calls) == 1
    else:
        method = (
            "prepare_fenced_response"
            if boundary == "before_tx1_forged"
            else "publish_fenced_response"
        )
        publish = getattr(runtime, method)

        def revoke_after_tx1(*args, **kwargs):
            revoked_epoch = kwargs["owner_witness"].owner_epoch
            revoke_and_restore()
            if boundary.endswith("forged"):
                # A same-UID caller can supply a fresh epoch in a dataclass.
                # The locked registry snapshot must attest that THIS turn was
                # created in that epoch, not merely compare caller assertions.
                new_epoch = comms.registry.snapshot().owner_epochs["beta"]
                assert new_epoch != revoked_epoch
                kwargs["owner_witness"] = replace(kwargs["owner_witness"], owner_epoch=new_epoch)
            return publish(*args, **kwargs)

        monkeypatch.setattr(runtime, method, revoke_after_tx1)
        with pytest.raises(StaleFence, match="turn stopped or changed"):
            await run_one_sealed_claim(
                root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            )
        assert len(calls) == 1
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT count(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute("SELECT count(*) FROM publication_receipts").fetchone()[0]
            == 0
        )
        obligation = store._connection.execute("SELECT state FROM obligations").fetchone()
        assert (obligation[0] if obligation is not None else None) == (
            "publishing"
            if boundary.startswith("after_tx1")
            else ("pending" if boundary in {"after_model", "before_tx1_forged"} else None)
        )


async def test_existing_owner_turn_is_preserved_after_success(tmp_path: Path, monkeypatch) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    comms.begin_turn("beta", "existing-real-turn")
    original = comms.registry.require("beta").active_turn
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(runtime, "run_native_pi_turn", runner)
    result = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    )
    assert result is not None and result.disposition is ClaimDisposition.COMPLETED
    assert len(calls) == 1
    assert comms.registry.require("beta").active_turn == original
    comms.finish_turn("beta", "existing-real-turn")


async def test_full_input_crash_leaves_no_publish_and_no_automatic_restart(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(fail_on=1)
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", runner)
    with pytest.raises(NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
    assert (
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        )
        is None
    )
    assert len(calls) == 1 and len(comms.bus.dm_history("sender", "beta")) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        row = store._connection.execute(
            "SELECT stage,session_id FROM native_runtime_inputs"
        ).fetchone()
        assert tuple(row) == ("full", None)
        assert store._connection.execute("SELECT state FROM obligations").fetchone()[0] == "pending"


def test_native_runtime_schema_explicit_install_and_drift_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with MutationStore(str(path)) as store:
        assert (
            store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='native_runtime_inputs'"
            ).fetchone()
            is None
        )
        install_native_runtime_schema(store)
        install_native_runtime_schema(store)
        assert_native_runtime_schema(store._connection)
        store._connection.execute("DROP TRIGGER native_runtime_input_delete_guard")
        with pytest.raises(PublicationActivationBlocked, match="drifted"):
            assert_native_runtime_schema(store._connection)


async def test_settled_page_does_not_hide_later_selected_claim(tmp_path: Path, monkeypatch) -> None:
    """Page saturation is not an empty inbox; scaffolding is not model authority."""
    root, root_id, comms, _initial, people = _root(tmp_path)
    lookup = stable_thread_lookup(people[1].created_at)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        for number in range(100):
            message = comms.send_initial_cohort(
                "sender", "#team", f"Bounded selected-page item {number}"
            )
            accept_initial_cohort(comms.bus, root_id, message.seq, store)
        first_page = sealed_cohort_claims(store, lookup, limit=100)
        assert len(first_page) == 100
        for claim in first_page:
            # Schema-legal terminal fixture only; no forged Pi context claim.
            store._connection.execute(
                "UPDATE wake_claims SET disposition='ignored',triage_verdict='ignore',"
                "revision=revision+1 WHERE claim_id=? AND disposition='triage_pending'",
                (claim.claim_id,),
            )
        assert len(sealed_cohort_claims(store, lookup, after_seq=first_page[-1].wire_seq)) == 1
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", runner)
    outcome = await run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    )
    assert outcome is not None and outcome.disposition is ClaimDisposition.IGNORED
    assert len(calls) == 1
    assert not (root / "read_markers.json").exists()


async def test_untrusted_pi_fails_before_any_bus_or_sql_mutation(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    with pytest.raises(NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id="0" * 32, owner_name="alpha", native_package=tmp_path
        )
    assert not list(root.iterdir())
