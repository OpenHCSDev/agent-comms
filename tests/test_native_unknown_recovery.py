"""Explicit abandonment releases a dead slot without replaying an UNKNOWN input."""

from __future__ import annotations

from agent_comms.lifecycle_capability import Failed
import asyncio
import multiprocessing
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.attempt_recovery import RecoveryMonitorCapability, VerifiedOwnerLoss
from agent_comms.bus import Bus
from agent_comms.input_disposition import InputDispositions
from agent_comms.private_path import FileRevision
from agent_comms.cli import main as cli_main
from agent_comms.cli_protocol import CoreError, WATCH_LINE
from agent_comms.native_arguments import GivenBinary
from agent_comms.launch_root import LaunchRoot
from agent_comms.runtime import socket_path
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_text import SavedSessionFile
from functools import partial
from native_backend_fixture import native_backend_fixture
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordination_tables.executions import CurrentExecutions, ExecutionOrigin
from agent_comms.cursor_publication import CursorDelivery
from agent_comms.native_input_record import FullNativeExecution, NumberNativeColumn, TextNativeColumn
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.selected_turn import SelectedAttempt
from agent_comms.launch_runtime import ThreadLaunch
from contextlib import closing
from test_coordinated_runtime import _root
from agent_comms.coordinator import Coordination
from agent_comms.execution_states import FailedExecution
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerReleaseEvidence
from agent_comms.reason_codes import GivenReasonCode
from test_native_failure_recovery import NativeSendHistory, UnknownHistory, release_native_input


async def release_starting_attempt(directory):
    root, root_id, comms, _initial, _people = _root(Path(directory), direct=True)
    comms.registry.register(replace(comms.registry.require("beta"), model=DeclaredModel("response-local/fixture")))
    with closing(CursorDelivery()) as delivery, Coordination(root / "coordination.sqlite3") as store:
        async with SelectedParticipant.select(
            comms, store, root_id, "beta", 0, cursor_delivery=delivery,
        ) as participant:
            assert participant is not None
            created = store.create(
                participant.batch.execution_id, ExecutionOrigin.WIRE,
                participant.lookup, participant.owner.thread.name, 1,
                sources=participant.batch.sources,
            ).value
            await SelectedAttempt.engage(participant, created)
            environment = ThreadLaunch.from_thread(participant.owner.thread).apply(dict(os.environ))
            os.environ.clear()
            os.environ.update(environment)
            comms.owners.release("beta")
    return root


def starting_owner(directory, output, exit_allowed):
    with closing(output):
        output.send(str(asyncio.run(release_starting_attempt(directory))))
    if not exit_allowed.wait(15):
        raise TimeoutError("test must release starting owner")


@pytest.fixture
def released_starting(tmp_path):
    context = multiprocessing.get_context("spawn")
    output, announcement = context.Pipe(duplex=False)
    exit_allowed = context.Event()
    process = context.Process(
        target=starting_owner, args=(str(tmp_path), announcement, exit_allowed)
    )
    process.start()
    announcement.close()
    with closing(output):
        try:
            root = Path(output.recv())
            leave(process, exit_allowed)
            yield root
        finally:
            leave(process, exit_allowed)


def recover_starting_owner(released_starting):
    """Use the owner recovery entrypoint; absence of an input never means unsent."""
    comms = Comms(released_starting)
    owner = comms.registry.require("beta")
    with Coordination(released_starting / "coordination.sqlite3") as store:
        pointer = CurrentExecutions.require(store.session._connection, owner_lookup=owner.incarnation.lookup)
        execution_id = pointer.execution_id.require("fixture must hold a starting attempt")
        before = store.get(execution_id)
        assert before.attempt.lifecycle.starting
        assert NativeRuntimeInput.count(store.session._connection) == 0
        inputs = input_evidence(store)
    bus = Bus(Bus.path_in(released_starting)).revision()
    dispositions = FileRevision.capture(InputDispositions.path_in(released_starting))
    RecoveryMonitorCapability.recover_owner(released_starting, owner)
    with Coordination(released_starting / "coordination.sqlite3") as store:
        after = store.get(execution_id)
        after.attempt.lifecycle.require(Failed, ValueError("failure required"))
        assert not after.is_current
        assert after.attempt.reason_code == GivenReasonCode("released_native_unknown")
        replay = after.replay.require_assessment()
        assert replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not replay.replay_safe
        assert not CurrentExecutions.require(store.session._connection,
            owner_lookup=owner.incarnation.lookup).unresolved
        assert input_evidence(store) == inputs
        assert NativeRuntimeInput.count(store.session._connection) == 0
    assert Bus(Bus.path_in(released_starting)).revision() == bus
    assert FileRevision.capture(InputDispositions.path_in(released_starting)) == dispositions


def test_recover_owner_without_an_input_settles_unknown_and_clears_pointer(released_starting):
    recover_starting_owner(released_starting)


def running_successor(comms):
    previous = comms.registry.require("beta")
    comms.registry.register(
        replace(previous, process_identity=ProcessIdentity.capture(os.getpid())),
        new_owner=True,
    )
    successor, _ = comms.registry.lease_local_turn("beta", "productive-successor")
    assert successor.executing
    return successor


def test_undispatched_original_recovers_beside_a_running_successor(released_starting):
    comms = Comms(released_starting)
    successor = running_successor(comms)
    recover_starting_owner(released_starting)
    assert comms.registry.require("beta") == successor
    assert successor.executing
    comms.registry.release_turn(successor.require_turn_lease())


@pytest.mark.parametrize("argument", ["--session", "--session-dir"])
def test_undispatched_owner_loss_refuses_a_live_native_child(released_starting, argument):
    comms = Comms(released_starting)
    owner = comms.registry.require("beta")
    directory = released_starting / "native-sessions" / owner.incarnation.lookup
    session = directory / "undispatched.jsonl"
    comms.registry.register(
        replace(owner, session_file=SavedSessionFile(str(session))),
        status=comms.registry.status(owner.name),
    )
    selected = session if argument == "--session" else directory
    child = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(30)", argument, str(selected),
    ])
    try:
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.recover_owner(released_starting, comms.registry.require("beta"))
        with Coordination(released_starting / "coordination.sqlite3") as store:
            pointer = CurrentExecutions.require(store.session._connection, owner_lookup=owner.incarnation.lookup)
            execution_id = pointer.execution_id.require("the live child must keep its attempt unresolved")
            assert store.get(execution_id).attempt.lifecycle.starting
            assert NativeRuntimeInput.count(store.session._connection) == 0
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_observed_owner_loss_expires_with_its_boundary(released_starting):
    owner = Comms(released_starting).registry.require("beta")
    with Coordination(released_starting / "coordination.sqlite3") as store:
        pointer = CurrentExecutions.require(store.session._connection, owner_lookup=owner.incarnation.lookup)
        execution_id = pointer.execution_id.require("fixture must hold a starting attempt")
        before = store.get(execution_id)
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as proof:
            assert proof.owns(store, before.attempt)
        assert not proof.owns(store, before.attempt)
        with pytest.raises(RecoveryBlocked):
            proof.require_native_exit()
        assert store.get(execution_id) == before


async def test_launch_recovers_before_reserving_replacement_pid(
    released_starting, native_runtime_package, source_programs,
):
    source_programs.bind(released_starting.parent)
    provider_root = released_starting.parent / "provider"
    provider_root.mkdir(mode=0o700)
    async with native_backend_fixture(provider_root, package=native_runtime_package):
        comms = Comms(released_starting)
        old = comms.registry.require("beta")
        with Coordination(released_starting / "coordination.sqlite3") as store:
            pointer = CurrentExecutions.require(store.session._connection, owner_lookup=old.incarnation.lookup)
            execution_id = pointer.execution_id.require("fixture must hold a starting attempt")
        with LaunchRoot.from_path(released_starting).selected():
            result = await Coordination.run_worker(partial(
                comms.owners.start, "beta", agent_bin=GivenBinary("pi"),
            ))
            owner = comms.registry.require("beta")
            try:
                async with asyncio.timeout(15):
                    while not socket_path(comms.root, result.pid).exists():
                        assert owner.process_alive
                        await asyncio.sleep(0.02)
                with Coordination(released_starting / "coordination.sqlite3") as store:
                    after = store.get(execution_id)
                    assert not after.is_current
                    assert after.attempt.reason_code == GivenReasonCode("released_native_unknown")
                    assert NativeRuntimeInput.count(store.session._connection) == 0
                assert owner.process_alive and owner.process_identity != old.process_identity
            finally:
                await Coordination.run_worker(partial(comms.owners.stop, "beta"))


def test_recover_owner_refuses_a_conflicting_input_identity(released_starting, capsys):
    comms = Comms(released_starting)
    owner = comms.registry.require("beta")
    with Coordination(released_starting / "coordination.sqlite3") as store:
        pointer = CurrentExecutions.require(store.session._connection, owner_lookup=owner.incarnation.lookup)
        execution_id = pointer.execution_id.require("fixture must hold a starting attempt")
        before = store.get(execution_id)
        with store.session.writing() as db:
            NativeRuntimeInput(
                input_id="c" * 32, stage=FullNativeExecution,
                execution_id=TextNativeColumn(execution_id),
                attempt_ordinal=NumberNativeColumn(before.attempt.attempt_ordinal),
                owner_lookup=owner.incarnation.lookup, owner_thread=owner.name,
                owner_generation=before.attempt.owner_generation + 1,
                owner_token_digest="e" * 64,
            ).insert(db)
        inputs = input_evidence(store)
    with pytest.raises(RecoveryBlocked) as blocked:
        RecoveryMonitorCapability.recover_owner(released_starting, owner)
    assert cli_main(["--root", str(released_starting), "start", "--name", "beta"]) == 1
    assert WATCH_LINE.validate_json(capsys.readouterr().out) == CoreError.from_exception(blocked.value)
    with Coordination(released_starting / "coordination.sqlite3") as store:
        assert store.get(execution_id) == before
        assert input_evidence(store) == inputs


def unknown_owner(directory, history, output, exit_allowed):
    source = asyncio.run(release_native_input(Path(directory), history))
    with closing(output):
        output.send((str(source.root), source.root_id, source.execution_id, source.input_id))
    if not exit_allowed.wait(15):
        raise TimeoutError("test must release fixture process")


@pytest.fixture(params=NativeSendHistory.members_with(UnknownHistory), ids=lambda history: history.declared_name)
def released_unknown(tmp_path, request):
    context = multiprocessing.get_context("spawn")
    output, announcement = context.Pipe(duplex=False)
    exit_allowed = context.Event()
    process = context.Process(
        target=unknown_owner, args=(str(tmp_path), request.param, announcement, exit_allowed)
    )
    process.start()
    announcement.close()
    with closing(output):
        try:
            root, root_id, execution_id, input_id = output.recv()
            yield process, exit_allowed, Path(root), root_id, execution_id, input_id
        finally:
            leave(process, exit_allowed)


def leave(process, exit_allowed):
    exit_allowed.set()
    process.join(5)
    if process.is_alive():
        process.terminate()
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
    before_bus = comms.bus.bus.revision()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        before_input = input_evidence(store)
        assert store.get(execution_id).is_current
    comms.owners.stop("beta")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        snapshot = store.get(execution_id)
        assert not snapshot.is_current
        assert type(snapshot.execution.lifecycle) is FailedExecution
        replay = snapshot.replay.require_assessment()
        assert replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not replay.replay_safe
        assert input_evidence(store) == before_input
    assert comms.bus.bus.revision() == before_bus
    assert input_id in str(before_input)


def test_abandon_refuses_released_but_live_owner(released_unknown):
    _process, _exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        assert store.get(execution_id).is_current


def test_abandon_refuses_live_native_process(released_unknown):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        session_dir = (
            root / "native-sessions" / store.get(execution_id).execution.owner_lookup
        )
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "--session-dir", str(session_dir)]
        )
        try:
            with pytest.raises(RecoveryBlocked):
                RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
            assert store.get(execution_id).is_current
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
        assert store.get(execution_id).is_current


def test_dead_original_settles_unknown_beside_a_live_successor(released_unknown):
    process, exit_allowed, root, _root_id, execution_id, _input_id = released_unknown
    leave(process, exit_allowed)
    comms = Comms(root)
    successor = running_successor(comms)
    before_bus = comms.bus.bus.revision()
    with Coordination(root / "coordination.sqlite3") as store:
        before_input = input_evidence(store)
        RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        after = store.get(execution_id)
        assert not after.is_current and isinstance(after.attempt.lifecycle, Failed)
        replay = after.replay.require_assessment()
        assert replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not replay.replay_safe
        assert input_evidence(store) == before_input
    assert comms.bus.bus.revision() == before_bus
    assert comms.registry.require("beta") == successor
    assert successor.executing
    comms.registry.release_turn(successor.require_turn_lease())


def test_unknown_settlement_is_atomic_on_existing_store(tmp_path):
    from agent_comms.coordination_errors import StaleFence, StaleRevision
    from test_coordination_store import ready, started

    with ready(tmp_path / "store.sqlite3") as store:
        _snapshot, fence = started(store)
        before = store.get(fence.execution_id)
        for stale_fence, revision, error in (
            (fence, before.pointer_revision + 1, StaleRevision),
            (replace(fence, token="wrong"), before.pointer_revision, StaleFence),
        ):
            with pytest.raises(error):
                store.fail_unknown(stale_fence, expected_pointer_revision=revision)
            assert store.get(fence.execution_id) == before

        after = store.fail_unknown(
            fence, expected_pointer_revision=before.pointer_revision
        ).value
        assert not after.is_current and not after.can_retry
        assert after.replay.require_assessment().facts & ReplayFact.UNKNOWN_EFFECTS
