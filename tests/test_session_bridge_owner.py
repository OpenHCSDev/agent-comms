"""The real SessionStart program must leave an installable serve after hook exit."""

from agent_comms.repo import Checkout
import asyncio
import json
import os
import select
import subprocess
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import IdentityMismatchError, ProcessIdentity
from agent_comms.cli_bridges import claude
from agent_comms.cli_bridges.monitor import InspectSession, VerifySession
from agent_comms.cli_bridges.shared import Binding, Serve
from conftest import installed_comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.root_runtime import RootRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms_hooks.session import started
from agent_comms_hooks.watch import LiveWatch, WatchClaim, WatchRecord
from agent_comms.threads import Thread


def test_session_start_hook_exit_preserves_the_recorded_owner_for_install_pause(
        tmp_path, bridge_home, source_programs):
    home = bridge_home
    session = 'copied-session'
    comms = installed_comms(tmp_path / 'bus')
    # The source programs run the hook's bridge, so their runtime owns the root it attaches to.
    RootRuntime.publish(comms.root, source_programs.runtime)
    comms.registry.declare(Thread(
        'bridge-copy', frozenset(), Checkout(tmp_path), execution=ExternalThreadExecution))
    transcript = tmp_path / f'{session}.jsonl'
    transcript.write_text('')
    base_state = WatchClaim.directory(home).parent / 'claude-bridge-owner'
    state = base_state / 'bridge-copy'
    binding = Binding(str(comms.root), 'bridge-copy', session, Binding.bus_head(str(comms.root)))
    with Binding.open(state) as db:
        binding.bind(db)
    claim = WatchClaim(session, home)
    claim.enrol(WatchRecord(binding.bus, binding.root, (
        str(source_programs.script(claude.main)), 'watch', '--state', str(state)), base_state))
    current = home / LaunchRuntime.current_pointer().relative_to(Path.home())
    current.parent.mkdir(parents=True)
    current.symlink_to(source_programs.prefix, target_is_directory=True)
    packet = tmp_path / 'session-start.json'
    packet.write_text(json.dumps(dict(
        session_id=session, hook_event_name='SessionStart', transcript_path=str(transcript))))
    env = {**os.environ, 'HOME': str(home), 'CLAUDE_CODE_SESSION_ID': session}
    env.pop('CLAUDE_PID', None)
    # An unenrolled hook must not even request owner authority.
    def unexpected_serve():
        raise AssertionError('Unclaimed serve was constructed')

    unclaimed = WatchClaim('not-enrolled', home)
    unclaimed.state().connect(
        unexpected_serve, lambda operation: VerifySession(owner=operation.owner))
    owner_log = tmp_path / 'owner.log'
    # This ordinary shell remains alive but is not a child subreaper. The hook
    # really exits, so its serve cannot retain the shell owner's ancestry.
    shell = (
        'export CLAUDE_PID=$$; "$1" < "$2" & hook=$!; '
        'wait "$hook" || exit "$?"; printf "%s\\n" "$hook"; read -r finish || :'
    )
    with owner_log.open('wb') as errors, subprocess.Popen(
            ('/bin/sh', '-c', shell, 'session-owner',
             str(source_programs.script(started)), str(packet)),
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors) as owner:
        try:
            assert select.select((owner.stdout,), (), (), 40)[0], owner_log.read_text()
            line = owner.stdout.readline()
            assert line, owner_log.read_text()
            hook_pid = int(line)
            expected = ProcessIdentity.capture(owner.pid)
            observed = InspectSession().information(claim.socket_path)
            assert observed.owner == expected
            assert owner.poll() is None
            assert observed.process.alive()
            with pytest.raises(ProcessLookupError):
                ProcessIdentity.capture(hook_pid)
            # This is exactly the relationship the old implementation rejected.
            with pytest.raises(IdentityMismatchError):
                expected.require_ancestor_of(observed.process.pid)
            with pytest.raises(ValueError, match='different'):
                VerifySession(owner=replace(expected, start_time=expected.start_time + 1)).exchange(
                    claim.socket_path)
            with pytest.raises(RuntimeError, match='older watch'):
                Serve.launch(claim, lambda: ProcessIdentity.capture(os.getpid()), transcript)
            assert observed.process.alive()
            (bridge,) = Serve.running(home)
            with ExitStack() as resources:
                paused = bridge.pause(resources)
                assert paused.bridge.observed.owner == expected
                assert isinstance(claim.state(), LiveWatch)
                assert owner.poll() is None
            # Installer EOF stops the retired process; it grants no replay or
            # fallback launch. Waiting on its identity does not require parenthood.
            asyncio.run(asyncio.wait_for(observed.process.wait_exited(), 15))
        finally:
            owner.stdin.close()
