"""Explicit abandonment releases a dead slot without replaying an UNKNOWN input."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.assignment_states import CompletedAssignment, FailedAssignment
from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.execution_states import FailedExecution
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.selected_request import SelectedRequest
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model, _root, tmp_path  # noqa: F401


def unknown_owner(directory, admitted, output, exit_allowed):
    # Reproduce the pre-fix durable UNKNOWN left by the historical worker.
    SelectedRequest._uncertain_failure = lambda self, error: None
    root, root_id, comms, _initial, _people = _root(Path(directory), direct=True)
    runtime._trusted_package = lambda path: path
    fake, _calls = _fake_model(fail_on=1)

    async def fail(*args, **kwargs):
        if admitted:
            await fake(*args, **kwargs)
        raise NativePiUnavailable("UNKNOWN before admission receipt")

    TrackedTurnSession.execute = fail
    try:
        asyncio.run(
            runtime.SelectedExecution(
                root=root, wire_root_id=root_id, owner_name="beta", native_package=Path("/unused")
            ).run()
        )
    except NativePiUnavailable:
        pass
    else:
        raise AssertionError("fixture must leave an unresolved native attempt")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        row = store.session._connection.execute(
            f"SELECT * FROM {NativeRuntimeInput.declared_name}"
        ).fetchone()
        execution_id, input_id = row["execution_id"], row["input_id"]
    os.environ["AGENT_COMMS_THREAD"] = "beta"
    comms.owners.release("beta")
    output.put((str(root), root_id, execution_id, input_id))
    if not exit_allowed.wait(15):
        raise TimeoutError("test must release fixture process")


@pytest.fixture(params=[False, True], ids=["missing-admission", "admitted-unknown"])
def released_unknown(tmp_path, request):  # noqa: F811
    context = multiprocessing.get_context("fork")
    output, exit_allowed = context.Queue(), context.Event()
    process = context.Process(
        target=unknown_owner, args=(str(tmp_path), request.param, output, exit_allowed)
    )
    process.start()
    try:
        root, root_id, execution_id, input_id = output.get(timeout=10)
        yield process, exit_allowed, Path(root), root_id, execution_id, input_id
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


def input_evidence(store):
    return {
        table: [tuple(row) for row in store.session._connection.execute("SELECT * FROM " + table)]
        for table in (NativeRuntimeInput.declared_name, CurrentNativeCursor.declared_name)
    }


@pytest.mark.asyncio
async def test_abandon_unknown_preserves_evidence_and_allows_only_new_work(
    released_unknown, monkeypatch
):
    process, exit_allowed, root, root_id, execution_id, input_id = released_unknown
    leave(process, exit_allowed)
    before_bus = (root / "bus.jsonl").read_bytes()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        before = input_evidence(store)
        result = RecoveryMonitorCapability.abandon_released_native_attempt(
            store, execution_id
        ).value
        assert type(result.execution.lifecycle) is FailedExecution
        assert result.attempt.lifecycle.failed
        assert not result.is_current and not result.can_retry
        assert result.replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not result.replay.replay_safe and result.replay.side_effects_possible
        assert all(type(row.lifecycle) is FailedAssignment for row in result.assignments)
        assert result.execution.reason_code == "released_native_unknown"
        assert result.publication_intent is result.publication_receipt is None
        assert input_evidence(store) == before
        assert (root / "bus.jsonl").read_bytes() == before_bus
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)

    # Real registry, private bus/cohort, slot acquisition and publication; only
    # the native model is fake. A different input must run, never the old one.
    comms = Comms(root, private_initial_writes=True)
    comms.registry.register(
        replace(
            comms.registry.require("beta"), process_identity=ProcessIdentity.capture(os.getpid())
        ),
        new_owner=True,
    )
    source = comms.messaging.send_initial_cohort("sender", "beta", "New independent request")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_delivery_cohort(comms.bus, root_id, source.seq, store)
    fake, calls = _fake_model()
    monkeypatch.setattr(runtime, "_trusted_package", lambda path: path)
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    result = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=Path("/unused")
    ).run()
    assert result.disposition is CompletedAssignment
    assert len(calls) == 1 and calls[0][0] != input_id
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert not store.snapshots.get(execution_id).can_retry
        assert (
            tuple(
                store.session._connection.execute(
                    f"SELECT * FROM {NativeRuntimeInput.declared_name} WHERE input_id=?",
                    (input_id,),
                ).fetchone()
            )
            == before[NativeRuntimeInput.declared_name][0]
        )


def test_abandon_refuses_released_but_live_owner(released_unknown):
    _process, _exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked, match="release does not prove loss"):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        assert store.snapshots.get(execution_id).is_current


def test_abandon_refuses_live_native_process(released_unknown):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        session_dir = (
            root / "native-sessions" / store.snapshots.get(execution_id).execution.owner_lookup
        )
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "--session-dir", str(session_dir)]
        )
        try:
            with pytest.raises(RecoveryBlocked, match="subprocess is still running"):
                RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
            assert store.snapshots.get(execution_id).is_current
        finally:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.parametrize(
    "damage", ["missing", "wrong-pid", "wrong-start-time", "wrong-identity", "wrong-epoch"]
)
def test_abandon_requires_real_release(released_unknown, damage):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    path = root / "owner_release_receipts.json"
    receipts = json.loads(path.read_text())
    if damage == "missing":
        receipts.clear()
    elif damage == "wrong-pid":
        receipts["beta"]["thread"]["process_identity"]["pid"] += 1
    elif damage == "wrong-start-time":
        receipts["beta"]["thread"]["process_identity"]["start_time"] += 1
    elif damage == "wrong-identity":
        receipts["beta"]["thread"]["created_at"] += 1
    else:
        receipts["beta"]["after"] = receipts["beta"]["before"]
    path.write_text(json.dumps(receipts))
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        assert store.snapshots.get(execution_id).is_current


def test_missing_admission_cannot_borrow_a_live_successor_release(released_unknown):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        if (
            store.session._connection.execute(
                f"SELECT sent_owner_admission_generation FROM {NativeRuntimeInput.declared_name}"
            ).fetchone()[0]
            is not None
        ):
            return  # Recorded admission already binds to the earlier attested release.
        comms = Comms(root)
        comms.registry.register(
            replace(
                comms.registry.require("beta"),
                process_identity=ProcessIdentity.capture(os.getpid()),
            ),
            new_owner=True,
        )
        with pytest.raises(RecoveryBlocked, match="release does not prove loss"):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        assert store.snapshots.get(execution_id).is_current


@pytest.mark.asyncio
async def test_real_local_rpc_failure_reaps_child_and_releases_slot(tmp_path, monkeypatch):  # noqa: F811
    from agent_comms import native_pi
    from agent_comms.durable_turn import DurableTurn

    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    processes = []
    from agent_comms.child_process import AttachedChild

    spawn = AttachedChild.start

    async def local_rpc(*_argv, **kwargs):
        # Actual pipes/reader/child cleanup, no native provider or credentials used.
        process = await spawn(
            (
                sys.executable,
                "-u",
                "-c",
                """
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"type":"response", "id":request["id"],
                  "command":"get_state", "success":False}), flush=True)
""",
            ),
            **kwargs,
        )
        processes.append(process)
        return process

    original_failure = DurableTurn.fail_unknown

    def after_reap(self):
        assert len(processes) == 1 and processes[0].returncode is not None
        return original_failure(self)

    monkeypatch.setattr(runtime, "_trusted_package", lambda path: path)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda path: Path("/bin/true"))
    monkeypatch.setattr(AttachedChild, "start", local_rpc)
    monkeypatch.setattr(DurableTurn, "fail_unknown", after_reap)
    with pytest.raises(NativePiUnavailable, match="capability is unavailable"):
        await runtime.SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
        ).run()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        old = store.session._connection.execute(
            f"SELECT * FROM {NativeRuntimeInput.declared_name}"
        ).fetchone()
        execution_id, old_id = old["execution_id"], old["input_id"]
        assert old["session_id"] is old["sent_owner_admission_generation"] is None
        snapshot = store.snapshots.get(execution_id)
        assert type(snapshot.execution.lifecycle) is FailedExecution
        assert not snapshot.is_current and not snapshot.can_retry
        assert snapshot.replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not snapshot.replay.replay_safe
    assert comms.registry.require("beta").active_turn is None
    source = comms.messaging.send_initial_cohort(
        "sender", "beta", "Independent after local failure"
    )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_delivery_cohort(comms.bus, root_id, source.seq, store)
    fake, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    result = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    ).run()
    assert result.disposition is CompletedAssignment
    assert len(calls) == 1 and calls[0][0] != old_id


@pytest.mark.asyncio
async def test_revoked_live_failure_keeps_slot_for_recovery(tmp_path, monkeypatch):  # noqa: F811
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda path: path)

    async def revoke_then_fail(*_args, **_kwargs):
        comms.registry.unregister("beta")
        raise NativePiUnavailable("original uncertain failure")

    monkeypatch.setattr(TrackedTurnSession, "execute", revoke_then_fail)
    with pytest.raises(NativePiUnavailable, match="original uncertain failure"):
        await runtime.SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
        ).run()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        row = store.session._connection.execute(
            f"SELECT execution_id FROM {NativeRuntimeInput.declared_name}"
        ).fetchone()
        assert store.snapshots.get(row[0]).is_current


def test_unknown_settlement_is_atomic_on_existing_store(tmp_path, monkeypatch):  # noqa: F811
    from agent_comms.coordination_errors import StaleFence, StaleRevision
    from test_coordination_store import ready, started

    with ready(tmp_path / "store.sqlite3") as store:
        _snapshot, fence = started(store)
        before = store.snapshots.get(fence.execution_id)
        for stale_fence, revision, error in (
            (fence, before.pointer_revision + 1, StaleRevision),
            (replace(fence, token="wrong"), before.pointer_revision, StaleFence),
        ):
            with pytest.raises(error):
                store.attempts.fail_unknown(stale_fence, expected_pointer_revision=revision)
            assert store.snapshots.get(fence.execution_id) == before

        def fail_settlement(*_args, **_kwargs):
            raise RuntimeError("settlement interrupted")

        with monkeypatch.context() as interrupted:
            interrupted.setattr(store.attempts, "settle_checked", fail_settlement)
            with pytest.raises(RuntimeError, match="settlement interrupted"):
                store.attempts.fail_unknown(
                    fence, expected_pointer_revision=before.pointer_revision
                )
        assert (
            store.snapshots.get(fence.execution_id) == before
        )  # replay + finality rolled back too.
        after = store.attempts.fail_unknown(
            fence, expected_pointer_revision=before.pointer_revision
        ).value
        assert not after.is_current and not after.can_retry
        assert after.replay.facts & ReplayFact.UNKNOWN_EFFECTS
