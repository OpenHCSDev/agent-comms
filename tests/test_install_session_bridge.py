"""Real session endpoint retirement; no live home, provider, or package mutation."""

import json
import os
import select
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges import claude
from agent_comms.cli_bridges.monitor import InspectSession, PauseSession
from agent_comms.cli_bridges.shared import (
    MONITOR_FRAME,
    Binding,
    BridgeResume,
    MonitorProbe,
    ResumedBridge,
    Serve,
    SessionBridge,
    UnobservedBridge,
)
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms_hooks.session import started
from agent_comms_hooks.watch import LapsedWatch, LiveWatch, WatchClaim, WatchRecord
from source_programs import SourcePrograms

SERVE_FROM_SOURCE = (
    sys.executable, '-c', 'from agent_comms.cli_bridges.claude import main; main()')


@dataclass(frozen=True)
class CopiedSession(SessionBridge):
    """The captured bridge plus its owned test process, binding and saved directory."""

    binding: Binding
    state: Path
    child: subprocess.Popen[bytes]
    log: Path


@contextmanager
def session_bridge(tmp_path, home, program=SERVE_FROM_SOURCE, *, session='copied-session'):
    comms = Comms(tmp_path / 'bus')
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread(
        'bridge-copy', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    transcript = tmp_path / f'{session}.jsonl'
    transcript.write_text('')
    state = home / '.local/state/agent-comms' / f'claude-bridge-{transcript.stem}'
    binding = Binding(str(comms.root), 'bridge-copy', transcript.stem)
    with Binding.open(state) as db:
        binding.bind(db)
    claim = WatchClaim(binding.thread, home)
    command = (str(Path(sys.executable).with_name('agent-comms-claude-bridge')),
               'watch', '--state', str(state))
    claim.enrol(WatchRecord(binding.bus, binding.root, command))
    owner = ProcessIdentity.capture(os.getpid())
    log = tmp_path / 'bridge.log'
    with log.open('wb') as output:
        child = subprocess.Popen(
            (*program, 'serve', '--state', str(state), '--owner',
             json.dumps(FieldCodec.encode_record(owner)), '--transcript', str(transcript)),
            env={**os.environ, 'HOME': str(home)}, stdout=output, stderr=output,
        )
    try:
        deadline = time.monotonic() + 20
        while child.poll() is None and time.monotonic() < deadline:
            try:
                observed = InspectSession().information(claim)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(.05)
        else:
            pytest.fail(log.read_text())
        yield CopiedSession(claim, observed, binding, state, child, log)
    finally:
        stop(child)


def test_copied_sessions_share_a_short_home_without_rebinding(tmp_path, bridge_home):
    with ExitStack() as resources:
        bridges = []
        for session in ('first-session', 'second-session'):
            directory = tmp_path / session
            directory.mkdir()
            bridges.append(resources.enter_context(
                session_bridge(directory, bridge_home, session=session)))
        assert len({bridge.claim.socket_path for bridge in bridges}) == len(bridges)
        for bridge in bridges:
            assert InspectSession().information(bridge.claim).process == bridge.observed.process
            assert bridge.child.poll() is None, bridge.log.read_text()
        assert {bridge.claim for bridge in Serve.running(bridge_home)} == {
            bridge.claim for bridge in bridges}


def stop(child):
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=15)


def probe(child):
    assert select.select((child.stdout,), (), (), 15)[0], 'Monitor did not attach'
    line = child.stdout.readline()
    assert line, child.stderr.read().decode()
    value = MONITOR_FRAME.validate_json(line)
    assert isinstance(value, MonitorProbe)
    return value


def test_real_monitor_survives_bridge_retirement_and_requires_a_fresh_probe(tmp_path, bridge_home):
    with session_bridge(tmp_path, bridge_home) as copied:
        env = {**os.environ, 'HOME': str(copied.claim.home)}
        with ExitStack() as cleanup:
            monitor = subprocess.Popen(
                (sys.executable, '-c',
                 'import asyncio; from agent_comms.cli_bridges.monitor import read_monitor; '
                 'from agent_comms_hooks.watch import WatchClaim; '
                 f'asyncio.run(read_monitor(WatchClaim({copied.claim.thread!r})))'),
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            cleanup.callback(stop, monitor)
            first = probe(monitor)
            (bridge,) = Serve.running(copied.claim.home)
            with ExitStack() as resources:
                bridge.pause(resources)
                assert monitor.poll() is None
            assert copied.child.wait(timeout=15) == 0, copied.log.read_text()
            with copied.log.open('ab') as output:
                successor = subprocess.Popen(
                    copied.child.args, env=env, stdout=output, stderr=output)
            cleanup.callback(stop, successor)
            second = probe(monitor)
            assert second.session == first.session
            assert second.token != first.token
            assert monitor.poll() is None


@pytest.mark.parametrize('wrong_identity', ('owner', 'process'))
def test_wrong_pause_identity_cannot_stop_the_real_session_bridge(
        tmp_path, bridge_home, wrong_identity):
    with session_bridge(tmp_path, bridge_home) as copied:
        other = ProcessIdentity.capture(os.getppid())
        request = PauseSession(
            owner=other if wrong_identity == 'owner' else copied.observed.owner,
            process=other if wrong_identity == 'process' else copied.observed.process,
        )
        with pytest.raises(ValueError, match='different'):
            request.exchange(copied.claim)
        assert InspectSession().information(copied.claim).process == copied.observed.process
        assert copied.child.poll() is None, copied.log.read_text()


def test_installer_pause_closes_the_real_bridge_and_eof_never_replays_or_relaunches(
        tmp_path, bridge_home):
    with session_bridge(tmp_path, bridge_home) as copied:
        with Binding.open(copied.state.with_name(copied.state.name + '-historical')) as db:
            copied.binding.bind(db)
        # Historical journals can name the same canonical live claim.
        (bridge,) = Serve.running(copied.claim.home)
        assert bridge.observed.process == copied.observed.process
        with ExitStack() as resources:
            paused = bridge.pause(resources)
            assert paused.bridge == bridge
            assert copied.child.poll() is None
            # The paused process keeps the claim; no session hook sees it lapse.
            assert isinstance(copied.claim.state(), LiveWatch)
            assert not copied.claim.socket_path.exists()
        # Losing the installer connection grants no fallback runtime or retry.
        assert copied.child.wait(timeout=15) == 0, copied.log.read_text()
        assert isinstance(copied.claim.state(), LapsedWatch)
        outcome = paused.resume()
        assert isinstance(outcome, UnobservedBridge)
        assert outcome.name == copied.claim.thread
        assert outcome.reason != UnobservedBridge(copied.claim.thread).reason
        assert FieldCodec.decode(BridgeResume, FieldCodec.encode(outcome)) == outcome
        with Binding.open(copied.state) as db:
            assert Binding.select(db)[0].thread == copied.claim.thread


def test_session_start_and_runtime_replacement_share_the_declared_launcher(tmp_path):
    owner = ProcessIdentity.capture(os.getpid())
    operation = Serve(owner=owner, transcript=tmp_path / 'copied-session.jsonl')
    record = WatchRecord('copy', '/copied/bus', ('/old/bin/agent-comms-claude-bridge',
                                             'watch', '--state', '/copied/state'))
    runtime = LaunchRuntime(tmp_path / 'runtime-B/bin/python')
    command = operation.command(record, runtime)
    assert command[:4] == (str(runtime.interpreter.with_name('agent-comms-claude-bridge')),
                           'serve', '--state', '/copied/state')
    assert FieldCodec.decode(ProcessIdentity, FieldCodec.parse(command[5])) == owner
    assert command[7] == str(operation.transcript)


def bridges_serving(state):
    """Every process serving this test's state; a second bridge would show here."""
    key = os.fsencode(state)
    serving = []
    for entry in Path('/proc').iterdir():
        try:
            arguments = (entry / 'cmdline').read_bytes().split(b'\0')
        except (NotADirectoryError, FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(argument.endswith(key) for argument in arguments):
            serving.append(int(entry.name))
    return serving


def test_install_swap_keeps_the_claim_and_resumes_the_same_process_on_the_new_runtime(
        tmp_path, bridge_home, monkeypatch):
    old = SourcePrograms.current(tmp_path / 'source-programs')
    new = LaunchRuntime.installed()
    with session_bridge(tmp_path, bridge_home, (str(old.script(claude.main)),)) as copied:
        current = copied.claim.home / LaunchRuntime.current_pointer().relative_to(Path.home())
        current.parent.mkdir(parents=True)
        current.symlink_to(new.prefix.absolute(), target_is_directory=True)
        monkeypatch.setenv('HOME', str(copied.claim.home))
        assert copied.observed.runtime.prefix == old.prefix.resolve()
        env = {**os.environ, 'HOME': str(copied.claim.home)}
        packet = json.dumps(dict(session_id=copied.claim.thread, hook_event_name='SessionStart',
                                 transcript_path=str(copied.observed.transcript)))
        (bridge,) = Serve.running(copied.claim.home)
        with ExitStack() as cleanup:
            with ExitStack() as resources:
                paused = bridge.pause(resources)
                assert not copied.claim.socket_path.exists()
                assert isinstance(copied.claim.state(), LiveWatch)
                hook = subprocess.run(
                    (str(old.script(started)),), input=packet, text=True, capture_output=True,
                    timeout=40, env={**env, 'CLAUDE_CODE_SESSION_ID': copied.claim.thread,
                                     'CLAUDE_PID': str(os.getpid())})
                assert hook.returncode != 0
                assert 'No second bridge launched' in hook.stdout + hook.stderr
                assert bridges_serving(copied.state) == [copied.child.pid]
                monitor = subprocess.Popen(
                    (sys.executable, '-c',
                     'import asyncio; from agent_comms.cli_bridges.monitor import read_monitor; '
                     'from agent_comms_hooks.watch import WatchClaim; '
                     f'asyncio.run(read_monitor(WatchClaim({copied.claim.thread!r})))'),
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                cleanup.callback(stop, monitor)
                outcome = paused.resume()
            resumed = InspectSession().information(copied.claim)
            assert outcome == ResumedBridge(copied.claim.thread, copied.observed.runtime,
                                           resumed.runtime)
            assert resumed.process == copied.observed.process
            assert resumed.process.pid == copied.child.pid
            assert resumed.runtime.prefix == new.prefix.resolve()
            state = copied.claim.state()
            assert isinstance(state, LiveWatch)
            assert Path(state.record.command[0]).resolve() == new.interpreter.with_name(
                old.script(claude.main).name).resolve()
            assert bridges_serving(copied.state) == [copied.child.pid]
            assert probe(monitor).session == copied.claim.thread
            assert copied.child.poll() is None, copied.log.read_text()
