"""Explicit abandonment releases a dead slot without replaying an UNKNOWN input."""

from __future__ import annotations
import asyncio
import multiprocessing
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.execution_states import FailedExecution
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerReleaseEvidence
from test_native_failure_recovery import NativeSendHistory, UnknownHistory, release_native_input
from test_coordinated_runtime import tmp_path  # noqa: F401


def unknown_owner(directory, history, output, exit_allowed):
    source = asyncio.run(release_native_input(Path(directory), history))
    output.put((str(source.root), source.root_id, source.execution_id, source.input_id))
    if not exit_allowed.wait(15):
        raise TimeoutError("test must release fixture process")


@pytest.fixture(params=NativeSendHistory.members_with(UnknownHistory), ids=lambda history: history.declared_name)
def released_unknown(tmp_path, request):  # noqa: F811
    context = multiprocessing.get_context("spawn")
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


def test_stop_of_previously_released_dead_owner_settles_without_replay(released_unknown):
    process, exit_allowed, root, _root_id, execution_id, input_id = released_unknown
    leave(process, exit_allowed)
    comms = Comms(root)
    before_bus = (root / "bus.jsonl").read_bytes()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        before_input = input_evidence(store)
        assert store.snapshots.get(execution_id).is_current
    comms.owners.stop("beta")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        snapshot = store.snapshots.get(execution_id)
        assert not snapshot.is_current
        assert type(snapshot.execution.lifecycle) is FailedExecution
        assert snapshot.replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not snapshot.replay.replay_safe
        assert input_evidence(store) == before_input
    assert (root / "bus.jsonl").read_bytes() == before_bus
    assert input_id in str(before_input)


def test_abandon_refuses_released_but_live_owner(released_unknown):
    _process, _exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked):
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
            with pytest.raises(RecoveryBlocked):
                RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
            assert store.snapshots.get(execution_id).is_current
        finally:
            child.terminate()
            child.wait(timeout=5)


def delete_release(db, original):
    OwnerReleaseEvidence.delete(db, name=original.name)


def change_release_pid(db, original):
    process = original.require_process()
    OwnerReleaseEvidence.update(db, where="name=?", parameters=(original.name,),
                                process_identity=replace(process, pid=process.pid + 1))


def change_release_start(db, original):
    process = original.require_process()
    OwnerReleaseEvidence.update(db, where="name=?", parameters=(original.name,),
                                process_identity=replace(process, start_time=process.start_time + 1))


def change_release_birth(db, original):
    OwnerReleaseEvidence.update(db, where="name=?", parameters=(original.name,), created_at=original.created_at + 1)


def invalidate_release_scope(db, original):
    OwnerReleaseEvidence.update(db, where="name=?", parameters=(original.name,), after=original.before)


@pytest.mark.parametrize("damage", [delete_release, change_release_pid, change_release_start,
                                    change_release_birth, invalidate_release_scope])
def test_abandon_requires_real_release(released_unknown, damage):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    releases = Comms(root).owners.releases
    original = releases.get("beta")
    with releases.writing() as db:
        damage(db, original)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        assert store.snapshots.get(execution_id).is_current


def test_missing_claim_cannot_borrow_a_live_successor_release(released_unknown):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        if (
            store.session._connection.execute(
                f"SELECT {NativeRuntimeInput.column_projection()['sent_owner_claimed_generation']} "
                f"FROM {NativeRuntimeInput.declared_name}"
            ).fetchone()[0]
            is not None
        ):
            return  # Recorded claim_scope already binds to the earlier attested release.
        comms = Comms(root)
        comms.registry.register(
            replace(
                comms.registry.require("beta"),
                process_identity=ProcessIdentity.capture(os.getpid()),
            ),
            new_owner=True,
        )
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        assert store.snapshots.get(execution_id).is_current


def test_unknown_settlement_is_atomic_on_existing_store(tmp_path):  # noqa: F811
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

        after = store.attempts.fail_unknown(
            fence, expected_pointer_revision=before.pointer_revision
        ).value
        assert not after.is_current and not after.can_retry
        assert after.replay.facts & ReplayFact.UNKNOWN_EFFECTS
