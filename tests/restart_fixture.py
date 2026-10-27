"""Original restart records from real registry declarations and a retired process."""

import subprocess
import sys

import pytest

import agent_comms.restart_queue as queue
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import RestartId
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.repo import Checkout
from agent_comms.threads import Thread

@pytest.fixture
def dead_owner_restart(private_comms):
    comms = private_comms
    comms.registry.declare(Thread('worker', frozenset(), Checkout(comms.root)))
    snapshot = comms.registry.snapshot()
    with subprocess.Popen(
        (sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'), stdin=subprocess.PIPE,
    ) as process:
        identity = ProcessIdentity.capture(process.pid)
        process.communicate(b'x', timeout=10)
        assert process.returncode == 0
    selection = OwnerRestartSelection(
        snapshot.owner_identity('worker'), identity,
        snapshot.claim_generation('worker'), sys.executable,
    )
    return queue.QueuedRestart(
        RestartId.mint(), selection,
        queue.RestartTarget(sys.executable, RestartEnvironment(), sys.executable),
        queue.PendingRestart(),
    )
