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

import agent_comms.coordinated_runtime as runtime
import agent_comms.native_pi as native_pi
from agent_comms.attempt_recovery import (
    RecoveryMonitorCapability,
    VerifiedOwnerLoss,
)
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.execution_states import FailedExecution
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerReleaseEvidence
from agent_comms.comms import Comms
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model, _root, tmp_path  # noqa: F401


def failed_owner(directory, output, exit_allowed):
    # Persist the historical pre-settlement failure shape for operator recovery.
    root, root_id, comms, _initial, _people = _root(Path(directory), direct=True)
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
        # Revoke the real owner before the failure consumer can settle. This
        # leaves the original authorized input unresolved without an obsolete
        # failure hook or a fabricated owner-loss grant.
        os.environ["AGENT_COMMS_THREAD"] = "beta"
        comms.owners.release("beta")
        raise NativePiUnavailable("released native provider failure")

    TrackedTurnSession.execute = old_failure
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
    with Coordination(str(root / "coordination.sqlite3")) as store:
        row = store.session._connection.execute(
            f"SELECT * FROM {NativeRuntimeInput.declared_name}"
        ).fetchone()
        execution_id, input_id = row["execution_id"], row["input_id"]
    os.environ["AGENT_COMMS_THREAD"] = "beta"
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
    with Coordination(str(root / "coordination.sqlite3")) as store:
        before = tuple(
            store.session._connection.execute(
                f"SELECT * FROM {NativeRuntimeInput.declared_name} WHERE input_id=?", (input_id,)
            ).fetchone()
        )
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as proof:
            assert proof.owns(store, store.snapshots.get(execution_id).attempt)
        assert not proof.owns(store, store.snapshots.get(execution_id).attempt)
        settled = RecoveryMonitorCapability.recover_native_failure(
            store, execution_id, session_file
        ).value
        assert type(settled.execution.lifecycle) is FailedExecution
        assert not settled.is_current
        assert settled.replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not settled.replay.replay_safe
        after = tuple(
            store.session._connection.execute(
                f"SELECT * FROM {NativeRuntimeInput.declared_name} WHERE input_id=?", (input_id,)
            ).fetchone()
        )
        assert after == before  # No forged context, cursor, or acceptance acknowledgement.
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)


def test_recovery_refuses_released_but_live_owner(released_failure):
    _process, _exit_allowed, root, execution_id, _input_id, session_file = released_failure
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked, match="release does not prove loss"):
            RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)
        assert store.snapshots.get(execution_id).is_current


@pytest.mark.parametrize("stop", ["stop", "error", "toolUse"])
def test_restart_recovery_preserves_native_outcome_without_replaying(released_failure, stop):
    from agent_comms.attempt_states import AttemptFailedAttempt, UnknownOutcomeAttempt
    from agent_comms.comms import Comms
    from agent_comms.restart_refusals import OwnerBusyRefusal

    process, exit_allowed, root, execution_id, input_id, session_file = released_failure
    leave(process, exit_allowed)
    rows = [json.loads(line) for line in session_file.read_text().splitlines()]
    rows[-1]["message"]["stopReason"] = stop
    session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    comms = Comms(root)
    owner = comms.registry.require("beta")
    with pytest.raises(OwnerBusyRefusal):
        comms.owners.require_restart_idle(owner)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        before = NativeRuntimeInput.one(store.session._connection, input_id=input_id)
        result = RecoveryMonitorCapability.recover_released_native_attempt(store, execution_id).value
        expected = UnknownOutcomeAttempt if stop == "toolUse" else AttemptFailedAttempt
        assert type(result.attempt.lifecycle) is expected
        assert not result.is_current
        assert not result.can_retry
        assert NativeRuntimeInput.one(store.session._connection, input_id=input_id) == before
        assert not result.publication_evidence
        assert not result.publication_intents
    comms.owners.require_restart_idle(owner)
    # Startup after settlement is a read, not another recovery or a replay.
    RecoveryMonitorCapability.recover_owner(root, owner)


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
        releases = Comms(root).owners.releases
        original = releases.get("beta")
        with releases.writing() as db:
            OwnerReleaseEvidence.update(db, where="name=?", parameters=("beta",), before=original.after + 1)
    elif damage == "wrong_session":
        session_file = session_file.parent.parent / "other.jsonl"
    if damage not in {"wrong_session", "wrong_epoch"}:
        session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with Coordination(str(root / "coordination.sqlite3")) as store:
        expected = ValueError if damage == "wrong_session" else (RecoveryBlocked, NativePiUnavailable)
        message = "Native identity differs from owner's canonical session" if damage == "wrong_session" else None
        with pytest.raises(expected, match=message):
            RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)
        assert store.snapshots.get(execution_id).is_current


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
        with Coordination(str(root / "coordination.sqlite3")) as store:
            with pytest.raises(RecoveryBlocked, match="subprocess is still running"):
                RecoveryMonitorCapability.recover_native_failure(store, execution_id, session_file)
            assert store.snapshots.get(execution_id).is_current
    finally:
        child.terminate()
        child.wait(timeout=5)



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


def test_later_attested_release_still_fences_original_claim(released_failure):
    process, exit_allowed, root, execution_id, _input_id, session_file = released_failure
    leave(process, exit_allowed)
    replacement = multiprocessing.get_context("fork").Process(
        target=replacement_release, args=(root,)
    )
    replacement.start()
    replacement.join(5)
    assert replacement.exitcode == 0
    with Coordination(str(root / "coordination.sqlite3")) as store:
        settled = RecoveryMonitorCapability.recover_native_failure(
            store, execution_id, session_file
        ).value
        assert type(settled.execution.lifecycle) is FailedExecution
        assert not settled.is_current
