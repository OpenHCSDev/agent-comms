"""Real session endpoint retirement; no live home, provider, or package mutation."""

import json
import os
import select
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.monitor import InspectSession, PauseSession
from agent_comms.cli_bridges.shared import MONITOR_FRAME, Binding, MonitorProbe, Serve
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms_hooks.watch import LapsedWatch, WatchClaim, WatchRecord


@pytest.fixture
def bridge_home(tmp_path, monkeypatch):
    # Relative socket paths fit sockaddr_un regardless of the checkout's length.
    # Each worker has its own alias, pointing only into its persistent test scratch.
    destination = tmp_path / 'home'
    destination.mkdir()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    home = Path(f'.bridge-home-{uuid.uuid4().hex[:8]}')
    home.symlink_to(destination, target_is_directory=True)
    try:
        yield home
    finally:
        home.unlink()


@contextmanager
def session_bridge(tmp_path, home):
    comms = Comms(tmp_path / 'bus')
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread(
        'bridge-copy', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    transcript = tmp_path / 'copied-session.jsonl'
    transcript.write_text('')
    state = home / '.local/state/agent-comms/claude-bridge-copy'
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
            (sys.executable, '-c',
             'from agent_comms.cli_bridges.claude import main; main()',
             'serve', '--state', str(state), '--owner',
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
        yield home, claim, observed, child, log
    finally:
        stop(child)


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
    with session_bridge(tmp_path, bridge_home) as (home, claim, _, child, log):
        env = {**os.environ, 'HOME': str(home)}
        with ExitStack() as cleanup:
            monitor = subprocess.Popen(
                (sys.executable, '-c',
                 'import asyncio; from agent_comms.cli_bridges.monitor import read_monitor; '
                 'from agent_comms_hooks.watch import WatchClaim; '
                 'asyncio.run(read_monitor(WatchClaim("copied-session")))'),
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            cleanup.callback(stop, monitor)
            first = probe(monitor)
            (bridge,) = Serve.running(home)
            with ExitStack() as resources:
                bridge.pause(resources)
                assert monitor.poll() is None
            assert child.wait(timeout=15) == 0, log.read_text()
            with log.open('ab') as output:
                successor = subprocess.Popen(child.args, env=env, stdout=output, stderr=output)
            cleanup.callback(stop, successor)
            second = probe(monitor)
            assert second.session == first.session
            assert second.token != first.token
            assert monitor.poll() is None


@pytest.mark.parametrize('wrong_identity', ('owner', 'process'))
def test_wrong_pause_identity_cannot_stop_the_real_session_bridge(
        tmp_path, bridge_home, wrong_identity):
    with session_bridge(tmp_path, bridge_home) as (_, claim, observed, child, log):
        other = ProcessIdentity.capture(os.getppid())
        request = PauseSession(
            owner=other if wrong_identity == 'owner' else observed.owner,
            process=other if wrong_identity == 'process' else observed.process,
        )
        with pytest.raises(ValueError, match='different'):
            request.exchange(claim)
        assert InspectSession().information(claim).process == observed.process
        assert child.poll() is None, log.read_text()


def test_installer_pause_closes_the_real_bridge_and_eof_never_replays_or_relaunches(
        tmp_path, bridge_home):
    with session_bridge(tmp_path, bridge_home) as (home, claim, observed, child, log):
        directory = home / '.local/state/agent-comms'
        with Binding.open(directory / 'claude-bridge-copy') as db:
            (binding,) = Binding.select(db)
        with Binding.open(directory / 'claude-bridge-historical-copy') as db:
            binding.bind(db)
        # Historical journals can name the same canonical live claim.
        (bridge,) = Serve.running(home)
        assert bridge.observed.process == observed.process
        with ExitStack() as resources:
            (paused,) = bridge.pause(resources)
            assert paused.bridge == bridge
            assert child.poll() is None
            assert isinstance(claim.state(), LapsedWatch)
            assert not claim.socket_path.exists()
        # Losing the installer connection grants no fallback runtime or retry.
        assert child.wait(timeout=15) == 0, log.read_text()
        with Binding.open(home / '.local/state/agent-comms/claude-bridge-copy') as db:
            assert Binding.select(db)[0].thread == claim.thread


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
