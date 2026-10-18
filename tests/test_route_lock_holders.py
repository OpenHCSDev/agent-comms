"""The installer names all scratch-route readers and waits for both to leave."""
import asyncio
import fcntl
import os
from contextlib import AsyncExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading

import agent_comms.active_route as active_route
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.child_process import AttachedChild
from agent_comms.comms import Comms


async def test_route_installer_names_and_waits_for_every_reader(tmp_path: Path, monkeypatch, capsys):
    comms = Comms(tmp_path / 'wire')
    root_id = comms.messaging.initialize_private_initial_protocol()
    route = active_route.ActiveRoute(comms.root, root_id)
    path = tmp_path / 'route-state' / 'active-route.json'
    path.parent.mkdir(mode=0o700)
    # This case exercises the real publisher and kernel locks, not SDK trust.
    monkeypatch.setattr(private_nk_entrypoint, 'pinned_native_package', lambda: tmp_path)
    script = tmp_path / 'bridge-watcher.py'
    script.write_text(
        'import fcntl, os, sys\n'
        'fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n'
        'fcntl.flock(fd, fcntl.LOCK_SH)\n'
        'sys.stdout.write("locked\\n"); sys.stdout.flush()\n'
        'sys.stdin.read(1)\n'
    )
    waiting = threading.Event()
    original_flock = fcntl.flock
    identity = path.parent.stat()

    def observed_flock(descriptor, operation):
        current = os.fstat(descriptor)
        if (operation == fcntl.LOCK_EX
                and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)):
            waiting.set()
        return original_flock(descriptor, operation)

    monkeypatch.setattr(active_route.fcntl, 'flock', observed_flock)
    # Children retire before the executor joins even if an assertion fails.
    with ThreadPoolExecutor(max_workers=1) as executor:
        async with AsyncExitStack() as lifetime:
            watchers = []
            for _ in range(2):
                watcher = await AttachedChild.start((sys.executable, str(script), str(path.parent)))
                lifetime.push_async_callback(watcher.stop)
                assert await asyncio.wait_for(watcher.stdout.readline(), 5) == b'locked\n'
                watchers.append(watcher)
            publishing = executor.submit(active_route.publish_active_route, route, path)
            assert await asyncio.to_thread(waiting.wait, 5)
            names = capsys.readouterr().err
            for watcher in watchers:
                assert f'PID {watcher.identity.pid}:' in names
            assert 'bridge-watcher.py' in names
            assert not publishing.done() and not path.exists()
            await watchers[0].write(b'x')
            assert (await asyncio.wait_for(watchers[0].wait(), 5)).successful
            assert not publishing.done() and not path.exists()
            await watchers[1].write(b'x')
            assert (await asyncio.wait_for(watchers[1].wait(), 5)).successful
            publishing.result(timeout=5)
            assert active_route.read_active_route(path) == route
