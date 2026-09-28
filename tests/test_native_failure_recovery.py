"""Released-owner recovery preserves unknown input evidence and refuses replay."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.assignment_states import FullPendingAssignment
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination import ReplayFact
from agent_comms.coordination_store import (
    MutationStore,
    RecoveryBlocked,
    RecoveryMonitorCapability,
    VerifiedOwnerLoss,
    _owner_loss_verified,
)
from agent_comms.execution_states import FailedExecution
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from test_coordinated_runtime import _fake_model, _root, tmp_path  # noqa: F401


def failed_owner(directory, output, exit_allowed):
    # Persist the historical pre-settlement failure shape for operator recovery.
    runtime.SelectedExecution._uncertain_failure = lambda self, error: None
    root, root_id, comms, _initial, _people = _root(Path(directory), direct=True)
    runtime._trusted_package = lambda path: path
    fake, _calls = _fake_model()

    async def old_failure(*args, **kwargs):
        result = await fake(*args, **kwargs)
        proof = result.context
        with proof.session_file.open("a") as target:
            target.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "error-terminal",
                        "parentId": proof.session_entry_id,
                        "message": {
                            "role": "assistant",
                            "stopReason": "error",
                            "errorMessage": "Codex error: The usage limit has been reached",
                            "content": [],
                        },
                    }
                )
                + "\n"
            )
        raise NativePiUnavailable("pre-fix native provider failure")

    runtime.run_native_pi_turn = old_failure
    try:
        asyncio.run(
            runtime.SelectedExecution(
                root=root, wire_root_id=root_id, owner_name="beta", native_package=Path("/unused")
            ).run()
        )
    except NativePiUnavailable:
        pass
    else:
        raise AssertionError("failure fixture unexpectedly succeeded")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        row = store._connection.execute(
            f"SELECT * FROM {NativeRuntimeInput.declared_name}"
        ).fetchone()
        execution_id, input_id = row["execution_id"], row["input_id"]
    os.environ["AGENT_COMMS_THREAD"] = "beta"
    comms.owners.release("beta")
    output.put((str(root), execution_id, input_id))
    if not exit_allowed.wait(10):
        raise TimeoutError("test failed to release fixture process")


@pytest.fixture
def released_failure(tmp_path):  # noqa: F811
    context = multiprocessing.get_context("fork")
    output, exit_allowed = context.Queue(), context.Event()
    process = context.Process(target=failed_owner, args=(str(tmp_path), output, exit_allowed))
    process.start()
    try:
        root, execution_id, input_id = output.get(timeout=10)
        root = Path(root)
        session_file = next((root / "native-sessions").glob("*/*.jsonl"))
        yield process, exit_allowed, root, execution_id, input_id, session_file
    finally:
        exit_allowed.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        output.close()
        assert process.exitcode == 0


def leave(process, exit_allowed):
    exit_allowed.set()
    process.join(5)
    assert process.exitcode == 0


def test_recovery_releases_only_failed_slot_and_never_recovers_acceptance(released_failure):
    process, exit_allowed, root, execution_id, input_id, session_file = released_failure
    leave(process, exit_allowed)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        before = tuple(
            store._connection.execute(
                f"SELECT * FROM {NativeRuntimeInput.declared_name} WHERE input_id=?", (input_id,)
            ).fetchone()
        )
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as proof:
            assert _owner_loss_verified(proof, execution_id, proof.owner_lookup, 1, 1, store)
        assert not _owner_loss_verified(proof, execution_id, proof.owner_lookup, 1, 1, store)
        settled = RecoveryMonitorCapability.recover_native_failure(
            store, execution_id, session_file
        ).value
        assert type(settled.execution.lifecycle) is FailedExecution
        assert not settled.is_current
        assert settled.replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not settled.replay.replay_safe
        after = tuple(
            store._connection.execute(
                f"SELECT * FROM {NativeRuntimeInput.declared_name} WHERE input_id=?", (input_id,)
            ).fetchone()
        )
        assert after == before  # No forged context, cursor, or acceptance receipt.
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)


def test_recovery_refuses_released_but_live_owner(released_failure):
    _process, _exit_allowed, root, execution_id, _input_id, session_file = released_failure
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked, match="release does not prove loss"):
            RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)
        assert store.snapshot(execution_id).is_current


@pytest.mark.parametrize(
    "damage",
    [
        "unfinished",
        "success",
        "different_parent",
        "additional_input",
        "tool",
        "wrong_epoch",
        "wrong_session",
    ],
)
def test_recovery_refuses_ambiguous_evidence(released_failure, damage):
    process, exit_allowed, root, execution_id, _input_id, session_file = released_failure
    leave(process, exit_allowed)
    rows = [json.loads(line) for line in session_file.read_text().splitlines()]
    if damage == "unfinished":
        rows.pop()
    elif damage == "success":
        rows[-1]["message"]["stopReason"] = "stop"
    elif damage == "different_parent":
        rows[-1]["parentId"] = "other"
    elif damage == "additional_input":
        rows.append({"type": "message", "message": {"role": "user", "content": []}})
    elif damage == "tool":
        rows[-1]["message"]["content"] = [{"type": "toolCall", "name": "bash"}]
    elif damage == "wrong_epoch":
        path = root / "owner_release_receipts.json"
        receipts = json.loads(path.read_text())
        receipts["beta"]["before"] = receipts["beta"]["after"] + 1
        path.write_text(json.dumps(receipts))
    elif damage == "wrong_session":
        session_file = session_file.parent.parent / "other.jsonl"
    if damage not in {"wrong_session", "wrong_epoch"}:
        session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        with pytest.raises((RecoveryBlocked, NativePiUnavailable)):
            RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)
        assert store.snapshot(execution_id).is_current


def test_recovery_refuses_live_native_session_process(released_failure):
    process, exit_allowed, root, execution_id, _input_id, session_file = released_failure
    leave(process, exit_allowed)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "--session-dir",
            str(session_file.parent),
        ]
    )
    try:
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            with pytest.raises(RecoveryBlocked, match="subprocess is still running"):
                RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)
            assert store.snapshot(execution_id).is_current
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.asyncio
async def test_unresolved_execution_does_not_engage_a_new_source(
    tmp_path,  # noqa: F811
    monkeypatch,
):
    from agent_comms.bus_publication import stable_thread_lookup
    from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_assignments
    from agent_comms.coordination_store import StaleFence

    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda path: path)
    fake, calls = _fake_model(fail_on=1)
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    # A historical unresolved attempt still blocks; current live failures are
    # settled separately by DurableTurn and do not produce this old shape.
    with monkeypatch.context() as historical:
        historical.setattr(
            runtime.SelectedExecution, "_uncertain_failure", lambda self, error: None
        )
        with pytest.raises(NativePiUnavailable):
            await runtime.SelectedExecution(
                root=root, wire_root_id=root_id, owner_name="beta", native_package=Path("/unused")
            ).run()
    source = comms.messaging.send_initial_cohort("sender", "beta", "New independent request")
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, source.seq, store)
        with pytest.raises(StaleFence, match="unresolved execution"):
            await runtime.SelectedExecution(
                root=root, wire_root_id=root_id, owner_name="beta", native_package=Path("/unused")
            ).run()
        claims = sealed_cohort_assignments(store, lookup, after_seq=source.seq - 1)
        assert len(claims) == 1
        assert type(claims[0].lifecycle) is FullPendingAssignment
        assert len(calls) == 1
        assert store._connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 1


def replacement_release(root):
    from dataclasses import replace

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms

    comms = Comms(root)
    owner = comms.registry.require("beta")
    comms.registry.register(
        replace(owner, process_identity=ProcessIdentity.capture(os.getpid())), new_owner=True
    )
    os.environ["AGENT_COMMS_THREAD"] = "beta"
    comms.owners.release("beta")


def test_later_attested_release_still_fences_original_admission(released_failure):
    process, exit_allowed, root, execution_id, _input_id, session_file = released_failure
    leave(process, exit_allowed)
    replacement = multiprocessing.get_context("fork").Process(
        target=replacement_release, args=(root,)
    )
    replacement.start()
    replacement.join(5)
    assert replacement.exitcode == 0
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        settled = RecoveryMonitorCapability.recover_native_failure(
            store, execution_id, session_file
        ).value
        assert type(settled.execution.lifecycle) is FailedExecution
        assert not settled.is_current
