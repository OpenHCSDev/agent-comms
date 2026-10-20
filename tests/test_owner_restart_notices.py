"""The committed native start event, not a connector observation, wakes held work."""

import os
import subprocess
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from test_commander_heartbeat import external_commander
from test_commander_problem_orders import create


@pytest.mark.parametrize("held", (False, True))
@pytest.mark.parametrize("previous_started", (False, True))
def test_owner_acquisition_announces_only_restarts_with_orders(commander, held, previous_started):
    comms, _ = commander
    if held:
        create(commander, "P1")
        create(commander, "P2")
    if previous_started:
        old = subprocess.Popen(["sleep", "30"])
        identity = ProcessIdentity.capture(old.pid)
        old.terminate()
        old.wait(timeout=5)
        comms.registry.register(
            replace(comms.registry.require("worker"), process_identity=identity), new_owner=True
        )
    with external_commander(comms, "lead"):
        owned = comms.owners.acquire_thread("worker", owner_pid=os.getpid())
        assert comms.registry.require("worker").process_identity == owned.process_identity
        messages = [m for m in comms.bus.inbox("worker") if m.sender == "commander-heartbeat"]
        assert len(messages) == int(held and previous_started)
        if messages:
            assert messages[0].body == (
                "Restarted holding order-P1, order-P2; continue them from your branch"
            )
        # The child's acquire after the parent reserved its process is not a second start.
        comms.owners.acquire_thread("worker", owner_pid=os.getpid())
        assert [
            m for m in comms.bus.inbox("worker") if m.sender == "commander-heartbeat"
        ] == messages
