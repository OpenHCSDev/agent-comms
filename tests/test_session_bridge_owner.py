"""The real SessionStart program must leave an installable serve after hook exit."""

from agent_comms.cli_bridges.session_watch import (
    LiveWatch,
    WatchClaim,
    WatchRecord,
)
from agent_comms.repo import Checkout
import asyncio
import json
import os
import select
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import IdentityMismatchError, ProcessIdentity
from agent_comms.cli_bridges import claude
from agent_comms.cli_bridges.monitor import InspectSession, VerifySession
from agent_comms.cli_bridges.shared import Binding, Serve
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.root_runtime import RootRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from agent_comms.cli_bridges import hook_session
from agent_comms_hooks.shell_commands import ConnectCommand
from agent_comms_hooks.watch import HookAnswer, SessionHookProgram
from agent_comms_hooks.read_gate import ReadGateProgram
from conftest import installed_comms


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
    binding = Binding(root=str(comms.root), owner=comms.registry.require('bridge-copy').incarnation,
                      thread=session, start=Binding.bus_head(str(comms.root)))
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
    dealt_output = tmp_path / 'session-deal.json'
    # This ordinary shell remains alive but is not a child subreaper. The hook
    # really exits, so its serve cannot retain the shell owner's ancestry.
    shell = (
        'export CLAUDE_PID=$$; "$1" < "$2" > "$3" & hook=$!; '
        'wait "$hook" || exit "$?"; printf "%s\\n" "$hook"; read -r finish || :'
    )
    with owner_log.open('wb') as errors, subprocess.Popen(
            ('/bin/sh', '-c', shell, 'session-owner',
             str(source_programs.script(hook_session.SessionHook.main)), str(packet), str(dealt_output)),
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors) as owner:
        try:
            assert select.select((owner.stdout,), (), (), 40)[0], owner_log.read_text()
            line = owner.stdout.readline()
            assert line, owner_log.read_text()
            hook_pid = int(line)
            context = json.loads(dealt_output.read_text())["hookSpecificOutput"]["additionalContext"]
            assert "Read each of these" in context
            assert transcript.with_suffix(".deal.json").exists()
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


@pytest.mark.parametrize("program", tuple(
    program for declaration in SessionHookProgram.descendants()
    for program in declaration.installation_members() if program.failure_status() == 2
))
@pytest.mark.parametrize(("suffix", "tool", "expected"), (
    ("", "Bash", 0),
    ("; echo unrelated", "Bash", 2),
    (" > unrelated-file", "Bash", 2),
    ("", "Read", 2),
))
def test_lapsed_hook_permission_admits_only_the_connect_declaration(
        tmp_path, bridge_home, source_programs, program, suffix, tool, expected):
    """The tool that repairs the bridge stays usable; another tool or shell effect does not."""
    session = 'lapsed-permission'
    claim = WatchClaim(session, bridge_home)
    claim.enrol(WatchRecord('copy', str(tmp_path / 'bus'), (), tmp_path / 'state'))
    command = f'{ConnectCommand.program.name} {ConnectCommand.command_name()}{suffix}'
    packet = dict(session_id=session, hook_event_name='PreToolUse',
                  transcript_path=str(tmp_path / 'session.jsonl'), tool_name=tool,
                  tool_input={'command': command})
    result = subprocess.run((str(source_programs.script(program.main)),),
                            input=json.dumps(packet), capture_output=True, text=True,
                            env={**os.environ, 'HOME': str(bridge_home)}, timeout=10)
    assert result.returncode == expected, result.stderr
    assert not result.stdout


def test_held_hook_waits_for_the_socket_successor(tmp_path, bridge_home, source_programs):
    """An exec-held claim's short socket gap is not an unbound session or a denied tool."""
    session = 'swapping-permission'
    claim = WatchClaim(session, bridge_home)
    claim.enrol(WatchRecord('copy', str(tmp_path / 'bus'), (), tmp_path / 'state'))
    packet = dict(session_id=session, hook_event_name='PreToolUse',
                  transcript_path=str(tmp_path / 'session.jsonl'), tool_name='Bash',
                  tool_input={'command': ':'})

    def successor():
        time.sleep(.2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.settimeout(10)
            server.bind(str(claim.socket_path))
            server.listen()
            with server.accept()[0] as peer, peer.makefile('rb') as stream:
                request = json.loads(stream.readline())
                assert request['type'] == ReadGateProgram.wire_name()
                peer.sendall(json.dumps({'type': HookAnswer.request,
                                         'stdout': 'successor answered\n'}).encode() + b'\n')

    try:
        with claim.claim.claimed(), ThreadPoolExecutor(1) as pool:
            served = pool.submit(successor)
            result = subprocess.run((str(source_programs.script(ReadGateProgram.main)),),
                                    input=json.dumps(packet), capture_output=True, text=True,
                                    env={**os.environ, 'HOME': str(bridge_home)}, timeout=15)
            served.result(timeout=10)
            assert result.returncode == 0, result.stderr
            assert result.stdout == 'successor answered\n'
    finally:
        claim.socket_path.unlink(missing_ok=True)
