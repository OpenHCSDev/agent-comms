"""Real release-before-exit: retirement keeps checking the exact owner fence."""

import os
import signal
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import (
    ExitedOutcome,
    ObservedProcess,
    ParentedProcess,
    SignaledOutcome,
)


@pytest.mark.asyncio
async def test_installed_idle_worker_retains_state_across_runtime_restart(tmp_path):
    """Actual installed workers; retained failure is observation, never input."""
    import asyncio
    import hashlib
    import json
    from contextlib import AsyncExitStack
    from agent_comms.child_process import BoundedRun
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_launch import RestartEnvironment, RetainedOwnerLaunch
    from native_backend_fixture import native_backend_fixture
    from delivery_owner_fixture import canonical_agent

    source_python = os.environ.get('IDLE_RESTART_SOURCE_PYTHON')
    retained_input = os.environ.get('IDLE_RESTART_RETAINED_INPUT')
    if not source_python or not retained_input:
        pytest.skip('Select the actual original runtime and retained input document')
    assert Path(sys.executable).parent != Path(source_python).parent
    original_document = InputDispositions(Path(retained_input)).read()
    assert original_document.rows and all(not row.accepts_reservation
                                         for row in original_document.rows.values())
    names = {row.owner for row in original_document.rows.values()}
    assert len(names) == 1
    name = names.pop()
    async with native_backend_fixture(tmp_path) as native, AsyncExitStack() as resources:
        comms = Comms(native.root)
        comms.owners.pin_private_nk_launch(native.root,
            os.environ['AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID'],
            Path(os.environ['PI_COMPACTION_TEST_PACKAGE']))

        async def retire_owned_worker():
            selected = comms.registry.snapshot().threads.get(name)
            if selected is not None and selected.process_alive:
                await asyncio.to_thread(comms.owners.stop, name)
            if selected is not None:
                assert not comms.registry.require(name).process_alive

        resources.push_async_callback(retire_owned_worker)
        environment = dict(os.environ)
        for key in ('PYTHONPATH', 'PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                    'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY'):
            environment.pop(key, None)
        environment.update(PATH=str(Path(source_python).parent)+os.pathsep+environment.get('PATH', ''),
                           VIRTUAL_ENV=str(Path(source_python).parent.parent))
        setup = await BoundedRun.run((source_python, '-B', '-c', '''
import json, os, sys
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.goal_actions import SetGoalAction, BlockedGoalAction
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RestartEnvironment
service = Comms(Path(os.environ['AGENT_COMMS_ROOT']))
with service.bus.log.locked(): root_id = service.bus.log._private_marker_unlocked().root_id
service.owners.pin_private_nk_launch(service.root, root_id,
    Path(os.environ['PI_COMPACTION_TEST_PACKAGE']))
name, project, session = sys.argv[1:]
service.registry.declare(Thread(name, frozenset(), project, session_file=session,
    model='response-local/fixture'))
service.threads.restore_stopped(service.registry.snapshot(), (name,))
service.goals.update_goal(name, SetGoalAction(text='Preserve this blocked private task'))
service.goals.update_goal(name, BlockedGoalAction(block_reason='Await original input disposition'))
arguments = ('--provider','response-local','--model','fixture','--thinking','off',
             '--offline','--no-extensions','--no-skills','--no-context-files',
             '--no-prompt-templates','--no-tools')
result = service.owners.start(name,
    agent_bin=str(Path(sys.executable).with_name('pi-comms-native')), agent_args=arguments)
print(json.dumps(FieldCodec.encode(result)), flush=True)
''', name, str(native.project), str(native.session)), timeout=20,
            cwd=native.project, env=environment)
        assert setup.outcome.successful, setup.stderr.decode()
        InputDispositions(native.root / InputDispositions.filename).replace(original_document)
        async def attach_current():
            current = comms.registry.require(name)
            from agent_comms.runtime import socket_path
            await asyncio.to_thread(wait_for, socket_path(native.root, current.pid))
            agent = canonical_agent(comms, auto_wake=False)
            try:
                async with asyncio.timeout(20):
                    await agent.load_session(str(native.project), name)
            finally:
                await agent.shutdown()
            return comms.registry.require(name)
        original = await attach_current()
        launch = RetainedOwnerLaunch.capture(original, comms.registry.snapshot(),
                                            interpreter=source_python)
        files = (native.session, native.config/'settings.json', native.config/'models.json',
                 native.config/'auth.json', native.root/InputDispositions.filename)
        baseline = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        target_environment = dict(environment)
        target_environment.update(PATH=str(Path(sys.executable).parent)+os.pathsep+os.defpath,
                                  VIRTUAL_ENV=str(Path(sys.executable).parent.parent))
        results = await asyncio.to_thread(comms.owners.restart_owners, (name,),
            agent_bin=str(Path(sys.executable).with_name('pi-comms-native')),
            runtime=RestartEnvironment.inherit(target_environment),
            source_interpreter=source_python)
        assert len(results) == 1 and results[0].previous_pid == original.pid
        assert not original.process_alive
        current = await attach_current()
        replacement = RetainedOwnerLaunch.capture(current, comms.registry.snapshot(),
                                                 interpreter=sys.executable)
        assert current.incarnation == original.incarnation
        assert current.worktree == original.worktree and current.session_file == original.session_file
        assert current.goal == original.goal and current.goal.state.declared_name == 'blocked'
        assert current.model == original.model and current.thinking_level == original.thinking_level
        assert current.turn_lease is None and native.provider.posts == 0
        assert replacement.arguments == launch.arguments
        assert RestartEnvironment.inherit(replacement.environment).native_config == RestartEnvironment.inherit(launch.environment).native_config
        assert baseline == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        print(json.dumps({'restart': FieldCodec.encode(results),
            'source_interpreter': launch.interpreter, 'target_interpreter': replacement.interpreter,
            'localhost_posts': native.provider.posts, 'retained_input_keys': list(original_document.rows),
            'journal_settings_input_unchanged': True, 'blocked_goal_preserved': True}), flush=True)
        assert native.provider.posts == 0
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import FencedOwnerBatch, StoppedOwnerFailure


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
    script = tmp_path / "owner.py"
    script.write_text("""import os, signal, time
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.threads import current_thread
root = Path(os.environ['AGENT_COMMS_ROOT'])
comms = Comms(root)
def release(signum, frame):
    comms.owners.release('worker')
    (root / 'released').touch()
    while not (root / 'exit').exists(): time.sleep(0.01)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, release)
comms.registry.declare(current_thread())
(root / 'ready').touch()
while True: time.sleep(0.01)
""")
    env = {
        **os.environ,
        "AGENT_COMMS_ROOT": str(root),
        "AGENT_COMMS_THREAD": "worker",
        "PI_AGENT_ID": "worker",
        "PI_WORKTREE": str(tmp_path),
        # This real source process must declare the command it actually runs.
        # Restart capture cannot infer extinct launch arguments from its PID.
        "AGENT_COMMS_AGENT_BIN": sys.executable,
        "AGENT_COMMS_AGENT_ARGS": shlex.join((str(script),)),
    }
    child = ParentedProcess.launch((sys.executable, str(script)), env=env)
    comms = Comms(root)
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
            ["worker"], agent_bin=sys.executable, agent_args=["-c", "print('local')"], **arguments
        )
        replacement = comms.registry.require("worker")
        assert replacement.process_alive
        assert replacement.process_identity != original.process_identity
        assert result.previous_pid == original.pid
    assert (comms.root / "released").exists()
    assert not child.alive()
    assert child.reap() == SignaledOutcome(signal.SIGKILL)
    receipt = comms.owners.releases.read()["worker"]
    assert receipt.thread.process_identity == original.process_identity
    assert receipt.after > receipt.before


def test_voluntary_exit_during_grace_is_reaped_without_force(releasing_owner):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.stop, "worker")
        wait_for(comms.root / "released")
        (comms.root / "exit").touch()
        stopping.result(timeout=5)
    assert child.reap() == ExitedOutcome(0)
    assert not child.alive()


@pytest.mark.parametrize("change", ["birth", "admission", "receipt"])
def test_changed_owner_after_release_refuses_escalation_and_replacement(releasing_owner, change):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.restart_owners, ["worker"], agent_bin=sys.executable)
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
        elif change == "admission":
            comms.registry.register(current, new_owner=True)
        else:
            comms.owners.releases.replace({})
        with pytest.raises(StoppedOwnerFailure) as caught:
            stopping.result(timeout=5)
        failure = caught.value
        try:
            assert isinstance(failure.__cause__, RelationViolationError)
            assert "changed while stopping" in str(failure.__cause__)
            phase = failure.stopped
            assert isinstance(phase, FencedOwnerBatch)
            assert len(phase.captured) == len(phase.unconfirmed) == 1
            assert phase.captured[0][0].process_identity == child.identity
            assert phase.exited_processes == phase.retired == ()
            assert phase.launches[0].process == child.identity
            assert child.alive(), "Stale fence must not authorize forced retirement"
            assert comms.registry.require("worker").pid == child.identity.pid
        finally:
            # Disposition precedes fixture teardown; no replacement or stale signal.
            failure.abandon()


@pytest.mark.asyncio
async def test_owner_that_fails_before_binding_releases_its_registration(tmp_path, monkeypatch):
    """A launcher registers the worker as running before it starts. If the worker
    fails before binding its session, shutdown still records that it stopped,
    instead of leaving a running thread whose process is gone."""
    from agent_comms.acp import CommsAgent
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.coordination_errors import PublicationActivationBlocked
    from agent_comms.threads import Thread

    comms = Comms(tmp_path, private_initial_writes=True)
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path),
                                  process_identity=ProcessIdentity.capture(os.getpid())))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    assert comms.registry.status("worker").running
    agent = CommsAgent(comms, runtime_enabled=True)
    # The 2026-09-29 startup failure: a private wire without a pinned launch.
    with pytest.raises(PublicationActivationBlocked):
        await agent.sessions.start_owner(str(tmp_path), "worker")
    await agent.shutdown()
    assert comms.registry.status("worker").stopped
