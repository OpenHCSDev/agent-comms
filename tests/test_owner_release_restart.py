"""Real release-before-exit: retirement keeps checking the exact owner fence."""

from agent_comms.native_arguments import GivenArguments, GivenBinary
from agent_comms.restart_scope import OwnersNamed
from agent_comms.turn_lease import NoTurnLease

import os
import signal
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from stored_inputs import stored_inputs

from agent_comms.child_process import (
    ExitedOutcome,
    ObservedProcess,
    SpawnedProcess,
    SignaledOutcome,
)


@pytest.mark.asyncio
async def test_installed_idle_worker_retains_state_across_runtime_restart(tmp_path, native_runtime_package):
    """Actual installed workers; retained failure is observation, never input."""
    import asyncio
    import hashlib
    import json
    from contextlib import AsyncExitStack
    from agent_comms.child_process import BoundedRun
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_launch import RetainedOwnerLaunch
    from agent_comms.launch_runtime import RestartEnvironment
    from native_backend_fixture import native_backend_fixture
    from delivery_owner_fixture import canonical_agent

    source_python = os.environ.get('IDLE_RESTART_SOURCE_PYTHON')
    retained_input = os.environ.get('IDLE_RESTART_RETAINED_INPUT')
    if not source_python or not retained_input:
        pytest.skip('Select the actual original runtime and retained input document')
    assert Path(sys.executable).parent != Path(source_python).parent
    original_document = stored_inputs(Path(retained_input))
    assert original_document.rows and all(not row.accepts_reservation
                                         for row in original_document.rows.values())
    names = {row.owner for row in original_document.rows.values()}
    assert len(names) == 1
    name = names.pop()
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native, AsyncExitStack() as resources:
        comms = wire(native.root)

        async def retire_owned_worker():
            selected = comms.registry.snapshot().threads.get(name)
            if selected is not None and selected.process_alive:
                await asyncio.to_thread(comms.owners.stop, name)
            if selected is not None:
                assert not comms.registry.require(name).process_alive

        resources.push_async_callback(retire_owned_worker)
        environment = dict(os.environ)
        for key in ('PYTHONPATH', 'PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK',
                    'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY'):
            environment.pop(key, None)
        environment.update(PATH=str(Path(source_python).parent)+os.pathsep+environment.get('PATH', ''),
                           VIRTUAL_ENV=str(Path(source_python).parent.parent))
        setup = await BoundedRun.run((source_python, '-B', '-c', '''
import json, os, sys
from pathlib import Path
from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.thread_model import DeclaredModel
from agent_comms.goal_actions import SetGoalAction, BlockedGoalAction
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import RestartEnvironment
service = wire(Path(os.environ['AGENT_COMMS_ROOT']))
name, project, session = sys.argv[1:4]
service.registry.declare(Thread(name, frozenset(), project,
    session_file=RegistrySessionLocation.from_path(session, service.root),
    model=DeclaredModel('response-local/fixture')))
service.registry.restore_stopped(service.registry.snapshot(), (name,))
service.goals.update_goal(name, SetGoalAction(text='Preserve this blocked private task'))
service.goals.update_goal(name, BlockedGoalAction(block_reason='Await original input disposition'))
arguments = ('--provider','response-local','--model','fixture','--thinking','off',
             '--offline','--no-extensions','--no-skills','--no-context-files',
             '--no-prompt-templates','--no-tools')
result = service.owners.start(name,
    agent_bin=str(Path(sys.executable).with_name('pi-comms-native')), agent_args=arguments)
print(json.dumps(FieldCodec.encode(result)), flush=True)
''', name, str(native.project), str(native.session), str(native_runtime_package.path)), timeout=20,
            cwd=native.project, env=environment)
        assert setup.outcome.successful, setup.stderr.decode()
        InputDispositions(native.root).replace(original_document)
        async def attach_current():
            current = comms.registry.require(name)
            from agent_comms.runtime import socket_path
            await asyncio.to_thread(wait_for, socket_path(native.root, current.require_process().pid))
            agent = canonical_agent(comms, auto_wake=False)
            try:
                async with asyncio.timeout(20):
                    await agent.load_session(str(native.project), name)
            finally:
                await agent.shutdown()
            return comms.registry.require(name)
        original = await attach_current()
        selection = OwnerRestartSelection.capture(comms.registry.snapshot(), name)
        assert selection.interpreter == source_python
        launch = RetainedOwnerLaunch.capture(selection, comms.registry.snapshot())
        files = (native.session, native.config/'settings.json', native.config/'models.json',
                 native.config/'auth.json', InputDispositions.path_in(native.root))
        baseline = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        target_environment = dict(environment)
        target_environment.update(PATH=str(Path(sys.executable).parent)+os.pathsep+os.defpath,
                                  VIRTUAL_ENV=str(Path(sys.executable).parent.parent))
        results = await asyncio.to_thread(comms.owners.restart_owners, OwnersNamed((name,)),
            agent_bin=GivenBinary(str(Path(sys.executable).with_name('pi-comms-native'))),
            runtime=RestartEnvironment.inherit(target_environment), expected=selection)
        assert len(results) == 1 and results[0].previous_pid == original.require_process().pid
        assert not original.process_alive
        current = await attach_current()
        replacement = RetainedOwnerLaunch.capture(
            OwnerRestartSelection.capture(comms.registry.snapshot(), name), comms.registry.snapshot())
        assert replacement.interpreter == sys.executable
        assert current.incarnation == original.incarnation
        assert current.worktree == original.worktree and current.session_file == original.session_file
        assert current.goal == original.goal and current.goal.state.declared_name == 'blocked'
        assert current.model == original.model and current.thinking_level == original.thinking_level
        assert current.turn_lease == NoTurnLease() and native.provider.posts == 0
        assert replacement.arguments == launch.arguments
        assert RestartEnvironment.inherit(replacement.environment).native_config == RestartEnvironment.inherit(launch.environment).native_config
        assert baseline == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        print(json.dumps({'restart': FieldCodec.encode(results),
            'source_interpreter': launch.interpreter, 'target_interpreter': replacement.interpreter,
            'localhost_posts': native.provider.posts, 'retained_input_keys': list(original_document.rows),
            'journal_settings_input_unchanged': True, 'blocked_goal_preserved': True}), flush=True)
        assert native.provider.posts == 0
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import FencedOwnerBatch, StoppedOwnerFailure
from agent_comms.store_files import ExclusiveLock, StoreLockContention, _store_lock
from agent_comms.threads import Thread


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"Owner did not produce {path.name}"
        time.sleep(0.01)


@pytest.fixture
def releasing_owner(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Release handler uses POSIX TERM")
    root = tmp_path / "wire"
    script = Path(__file__).with_name('releasing_owner_child.py')
    arguments = (str(script), str(root))
    env = {
        **os.environ,
        "AGENT_COMMS_THREAD": "worker",
        "PI_WORKTREE": str(tmp_path),
        # This real source process must declare the command it actually runs.
        # Restart capture cannot infer extinct launch arguments from its PID.
        "AGENT_COMMS_AGENT_BIN": sys.executable,
        "AGENT_COMMS_AGENT_ARGS": shlex.join(arguments),
    }
    child = SpawnedProcess.launch((sys.executable, *arguments), env=env)
    comms = wire(root)
    try:
        wait_for(root / "ready")
        yield comms, child
    finally:
        # A release handler deliberately resists TERM to exercise escalation.
        if child.alive():
            child.force()
        child.reap()
        current = comms.registry.snapshot().threads.get("worker")
        if (
            current is not None
            and current.process_alive
            and current.process_identity != child.identity
        ):
            ObservedProcess(current.process_identity).stop_sync()


def test_stop_before_first_worker_keeps_the_exact_owner_fence(tmp_path):
    from agent_comms.child_process import NoProcess, ProcessIdentity

    comms = wire(tmp_path / "wire")
    declared = comms.registry.declare(Thread('worker', frozenset(), str(tmp_path)))
    generation = comms.registry.snapshot().claim_generation(declared.name)
    comms.owners.stop(declared.name)
    snapshot = comms.registry.snapshot()
    assert snapshot.statuses[declared.name].stopped
    assert snapshot.threads[declared.name].process_identity == NoProcess()
    assert comms.owners.releases.read_many((declared.name,))[declared.name].current(snapshot, declared, generation)

    waiting = comms.registry.declare(Thread('waiting', frozenset(), str(tmp_path)))
    before = comms.registry.snapshot().claim_generation(waiting.name)
    comms.registry.register(
        replace(waiting, process_identity=ProcessIdentity.capture(os.getpid())),
        new_owner=True,
    )
    with pytest.raises(RelationViolationError):
        comms.registry.snapshot().require_stopping_owner(waiting, before)


def test_stop_of_crashed_owner_records_exact_release(releasing_owner):
    comms, child = releasing_owner
    original = comms.registry.require("worker")
    child.force()
    child.reap()
    assert not original.process_alive
    comms.owners.stop("worker")
    snapshot = comms.registry.snapshot()
    acknowledgement = comms.owners.releases.read_many(("worker",))["worker"]
    assert acknowledgement.current(snapshot, original, acknowledgement.before)
    assert snapshot.statuses["worker"].stopped


@pytest.mark.parametrize("mode", ["restart", "guarded", "stop"])
def test_released_process_must_exit_before_replacement(releasing_owner, mode):
    comms, child = releasing_owner
    original = comms.registry.require("worker")
    arguments = {}
    if mode == "guarded":
        snapshot = comms.registry.snapshot()
        arguments["expected"] = OwnerRestartSelection.capture(snapshot, "worker")
    if mode == "stop":
        comms.owners.stop("worker")
    else:
        (result,) = comms.owners.restart_owners(
            OwnersNamed(("worker",)), agent_bin=GivenBinary(sys.executable), agent_args=GivenArguments(("-c", "print('local')",)), **arguments
        )
        replacement = comms.registry.require("worker")
        assert replacement.process_alive
        assert replacement.process_identity != original.process_identity
        assert result.previous_pid == original.require_process().pid
    assert (comms.root / "released").exists()
    assert not child.alive()
    assert child.reap() == SignaledOutcome(signal.SIGKILL)
    acknowledgement = comms.owners.releases.read_many(("worker",))["worker"]
    assert acknowledgement.process_identity == original.process_identity
    assert acknowledgement.after > acknowledgement.before


def test_voluntary_exit_during_grace_is_reaped_without_force(releasing_owner):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.stop, "worker")
        wait_for(comms.root / "released")
        (comms.root / "exit").touch()
        stopping.result(timeout=5)
    assert child.reap() == ExitedOutcome(0)
    assert not child.alive()


@pytest.mark.parametrize("change", ["birth", "claim_scope", "acknowledgement"])
def test_changed_owner_after_release_refuses_escalation_and_replacement(releasing_owner, change):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.restart_owners, OwnersNamed(("worker",)), agent_bin=GivenBinary(sys.executable))
        wait_for(comms.root / "released")
        current = comms.registry.require("worker")
        if change == "birth":
            comms.registry.register(
                replace(
                    current,
                    process_identity=replace(
                        child.identity, start_time=child.identity.start_time + 1
                    ),
                ),
                new_owner=True,
            )
        elif change == "claim_scope":
            comms.registry.register(current, new_owner=True)
        else:
            comms.owners.releases.remove_threads((current,))
        with pytest.raises(StoppedOwnerFailure) as caught:
            stopping.result(timeout=5)
    failure = caught.value
    assert isinstance(failure.__cause__, RelationViolationError)
    # Toad shows a RuntimeError on the thread; its text names the cause.
    assert isinstance(failure, RuntimeError) and "changed while stopping" in str(failure)
    phase = failure.stopped
    assert isinstance(phase, FencedOwnerBatch)
    assert len(phase.captured) == len(phase.unconfirmed) == 1
    assert phase.captured[0][0].process_identity == child.identity
    assert phase.retired == ()
    assert phase.launches[0].process == child.identity
    assert child.alive(), "Stale fence must not authorize forced retirement"
    assert comms.registry.require("worker").require_process().pid == child.identity.pid
    with _store_lock(comms.bus.bus.path, ExclusiveLock(), StoreLockContention(0)):
        pass  # the failed restart released the wire itself


