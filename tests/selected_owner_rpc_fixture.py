"""A real selected owner and child pipes, with a recorded RPC executable."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.native_package import pinned_native_package
from agent_comms.private_nk_entrypoint import PrivateNkLaunch


@pytest.fixture
async def scripted_owner(tmp_path, selected_model, monkeypatch):
    control = tmp_path / 'rpc-control'
    control.mkdir()
    os.mkfifo(control / 'entered')
    tests = Path(__file__).parent
    source = tests.parent / 'src'
    child = tests / 'selected_owner_rpc_child.py'
    program = (f'import sys,runpy; sys.path[:0]=[{str(source)!r},{str(tests)!r}]; '
               f'runpy.run_path({str(child)!r},run_name="__main__")')
    cli = control / 'cli.mjs'
    cli.write_text(
        "import {spawnSync} from 'node:child_process';\n"
        f"const child=spawnSync({json.dumps(sys.executable)},"
        f"['-c',{json.dumps(program)},{json.dumps(str(control))},...process.argv.slice(2)],"
        "{stdio:'inherit'}); process.exit(child.status??1);\n"
    )
    # This explicitly supplied executable is a protocol fixture, not a reviewed
    # Pi package. Do not claim package/import-fence verification from this test.
    for name in ('agent-comms-import-fence.mjs', 'agent-comms-project-bootstrap.mjs'):
        (control / name).write_text('')
    package = pinned_native_package().resolve(strict=True)
    comms = Comms(tmp_path / 'wire')
    for name in ('sender', 'beta'):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset({'team'}), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()), model='test/model',
        ))
    root_id = comms.messaging.initialize_private_initial_protocol()
    with Coordination(str(comms.root / 'coordination.sqlite3')) as store:
        owner = comms.registry.require('beta')
        store.participants.register(RecordedThreadIncarnation('fixture', owner.created_at).lookup,
                                    'beta', 'beta', committed=True)
    comms.owners.pin_private_nk_launch(PrivateNkLaunch(comms.root, root_id, package))
    agent = CommsAgent(comms, runtime_enabled=True,
                       private_nk_verified_cli=cli)
    agent.sessions.bindings['beta'] = 'beta'
    agent.sessions.titles['beta'] = 'beta'
    agent.sessions.worktrees['beta'] = str(tmp_path)
    # The test owns one explicit observer; ACP input must not start another.
    monkeypatch.setattr(agent.inputs, 'ensure_live_drain', lambda _: None)
    await agent._runtime.start()
    try:
        yield comms, agent, control
    finally:
        (control / 'release').touch()
        await agent.shutdown()


async def recorded_request(control, agent):
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    fd = os.open(control / 'entered', os.O_RDONLY | os.O_NONBLOCK)
    try:
        async with asyncio.timeout(15):
            loop.add_reader(fd, entered.set_result, None)
            await entered
    except TimeoutError:
        from delivery_owner_fixture import drain_diagnostic

        raise AssertionError(f'Scripted input never arrived: {drain_diagnostic(agent._comms, "beta")!r}') from None
    finally:
        loop.remove_reader(fd)
        os.close(fd)


def requests(control):
    path = control / 'requests.jsonl'
    return tuple(map(json.loads, path.read_text().splitlines())) if path.exists() else ()
