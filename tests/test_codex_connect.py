"""Connect binds a real Codex socket once; systemd resumes that exact command."""
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions, Connect, Resume
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.cli_bridges.shared import Binding, Watch
from agent_comms.execution.session_file import BusSessionLocation
from agent_comms.host_services.codex_inbox import CodexInboxService
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread
from conftest import installed_comms


@pytest.fixture
async def codex_socket(tmp_path):
    async def serve(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            if frame['method'] == 'initialized':
                continue
            if frame['method'] == 'initialize':
                result = {'userAgent': 'codex/connect-test'}
            else:
                assert frame['method'] == 'thread/resume'
                result = {'thread': {'id': 'session', 'cwd': str(tmp_path),
                                     'status': {'type': 'idle'}}}
            await socket.send_json({'id': frame['id'], 'result': result})
        return socket

    app = web.Application()
    app.router.add_get('/', serve)
    runner = web.AppRunner(app)
    await runner.setup()
    socket_path = tmp_path.parent / 'codex-connect.sock'
    await web.UnixSite(runner, str(socket_path)).start()
    try:
        yield str(socket_path)
    finally:
        await runner.cleanup()


async def test_connect_and_service_resume_keep_the_actual_command(
        tmp_path, monkeypatch, codex_socket, installed_runtime):
    monkeypatch.chdir(tmp_path)
    comms = installed_comms(tmp_path / 'bus')
    started = []
    monkeypatch.setattr(CodexInboxService, 'connect', lambda self: started.append(self.session))
    command = ['agent-comms-codex-bridge', 'connect', '--root', str(comms.root),
               '--bus-thread', 'cli', '--session', 'session',
               '--state', 'custom-state', '--socket', '../codex-connect.sock']
    monkeypatch.setattr(sys, 'argv', command)
    options = CodexOptions.parse(None, CodexBridge.state_path(), CodexBridge)
    assert isinstance(options.operation, Connect)
    with Binding.open(options.state) as db:
        bridge = CodexBridge(options, options.binding.bind(db), db)
        await options.operation.prepare(bridge)
        await options.operation.execute(bridge)
    saved = WatchClaim('session').recorded()
    assert started == ['session']
    monkeypatch.setattr(sys, 'argv', list(saved.command))
    watching = CodexOptions.parse(None, CodexBridge.state_path(), CodexBridge)
    assert type(watching.operation) is Watch
    assert Path(watching.socket).resolve() == Path(codex_socket)
    assert watching.state == tmp_path / 'custom-state' / 'cli'
    assert saved.base_state == tmp_path / 'custom-state'
    assert comms.registry.require('cli').worktree == Checkout(tmp_path)

    # The service gets only its session. Its original binding supplies every other argument.
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(sys, 'argv', [command[0], 'resume', '--session', 'session'])
    options = CodexOptions.parse(None, CodexBridge.state_path(), CodexBridge)
    assert isinstance(options.operation, Resume)
    resumed = []
    class ReplacedProcessError(Exception):
        pass
    def execute(path, arguments):
        resumed.append((Path(path), arguments))
        raise ReplacedProcessError
    monkeypatch.setattr('agent_comms.cli_bridges.codex.os.execv', execute)
    with Binding.open(options.state) as db:
        bridge = CodexBridge(options, options.binding.bind(db), db)
        await options.operation.prepare(bridge)
        with pytest.raises(ReplacedProcessError):
            await options.operation.execute(bridge)
    assert resumed[0][1][1:] == saved.command[1:]
    assert resumed[0][0].name == CodexInboxService.program


async def test_connect_refuses_a_native_history_before_enrolling_or_starting(
        tmp_path, monkeypatch, codex_socket):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    comms = installed_comms(tmp_path / 'bus')
    original = comms.registry.declare(ExternalThread(
        'imported', frozenset(), Checkout(tmp_path),
        session_file=BusSessionLocation(Path('imported.jsonl')).bound_to(comms.root)))
    started = []
    monkeypatch.setattr(CodexInboxService, 'connect', lambda self: started.append(self.session))
    monkeypatch.setattr(sys, 'argv', ['agent-comms-codex-bridge', 'connect',
        '--root', str(comms.root), '--bus-thread', 'imported', '--session', 'session',
        '--socket', codex_socket])
    options = CodexOptions.parse(None, CodexBridge.state_path(), CodexBridge)
    with Binding.open(options.state) as db:
        bridge = CodexBridge(options, options.binding.bind(db), db)
        with pytest.raises(
                ValueError, match='native session cannot publish an external transcript'):
            await options.operation.prepare(bridge)
    assert started == []
    assert not WatchClaim('session').record_path.exists()
    assert comms.registry.require('imported').session_file == original.session_file
