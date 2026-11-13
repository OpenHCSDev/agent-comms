"""A reader exit is not concealed by a still-living shared Codex server."""
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_status import NoBridgeThreadStatus
from agent_comms.registry.threads import ExternalThread
from conftest import installed_comms


def test_inbox_lifetime_follows_the_reader_not_the_shared_server(tmp_path):
    comms = installed_comms(tmp_path / 'root')
    comms.registry.declare(ExternalThread('cli', frozenset(), Checkout(tmp_path)))
    server = ProcessIdentity.capture(os.getpid())
    socket_path = tmp_path.parent / 'reader.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(socket_path))
        endpoint.listen(1)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('bridge_reader_probe.py')), str(comms.root), str(socket_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 15)
            assert ready, 'The actual reader did not finish startup'
            assert process.stdout.readline().strip() == 'ready'
            reader = ProcessIdentity.capture(process.pid)
            assert comms.registry.require('cli').process_identity == reader
            assert comms.registry.snapshot().status('cli').is_receiving()
            process.kill()
            process.wait(timeout=5)
            assert server.alive()
            assert isinstance(comms.registry.snapshot().status('cli'), NoBridgeThreadStatus)
            row = comms.views.presentation.roster('cli').threads['cli']
            assert not row.presentation.status.is_working()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
