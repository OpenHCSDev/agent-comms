"""Reader liveness is socket attachment, not message consumption.

A reader reads `connected` from its bridge's attachment — the registry's live process binding
and the Monitor probe — never from whether it has drained its inbox. An unread message is the
read receipt's business: it shows in the pending count, not as a `not reading` overlay on a
live reader. This is the inverse of the 2026-11-04 stall heuristic, which read a
SIGSTOP-frozen-but-alive bridge as `not reading`; that inference was redundant with the read
receipt and false on any session that works in turn-bursts with idle gaps.
"""

import os
import signal
import subprocess
from collections.abc import Iterator

import pytest

from agent_comms.comms import wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.registry.activity import ReadyDrainReadiness
from agent_comms.team.standing import UnitDeaf
from test_cli import cli  # noqa: F401  (the installed-root command line fixture)


@pytest.fixture
def bridge_process() -> Iterator[subprocess.Popen]:
    """A real process standing in for the session bridge the registry binds."""
    child = subprocess.Popen(["sleep", "600"])
    try:
        yield child
    finally:
        child.send_signal(signal.SIGCONT)
        child.kill()
        child.wait()


@pytest.fixture
def connected(cli, tmp_path, bridge_process):  # noqa: F811
    """Thread `bridge` bound to the real process, as a connected inbox; `sender` writes."""
    for name in ("bridge", "sender"):
        cli(tmp_path, "register", "--name", name, "--worktree", "/wt")
    with wire(tmp_path).registry.external_process(
            "bridge", ProcessIdentity.capture(bridge_process.pid)):
        yield wire(tmp_path)


def readiness(comms):
    return comms.agents.activity_of("bridge", snapshot=comms.registry.snapshot()).readiness


def test_unread_does_not_mark_a_connected_reader_not_reading(connected):
    connected.messaging.send("sender", "bridge", "waiting to be read")
    # The reader has taken nothing, yet its bridge is bound, so it reads ready, not stalled.
    assert type(readiness(connected)) is ReadyDrainReadiness
    # The unread message is the read receipt's business: it shows in the pending count.
    assert connected.bus.pending_count("bridge") == 1
    assert UnitDeaf.violations(connected) == ()


def test_a_frozen_but_attached_bridge_still_reads_connected(connected, bridge_process):
    os.kill(bridge_process.pid, signal.SIGSTOP)
    connected.messaging.send("sender", "bridge", "probe while frozen")
    # A frozen process whose socket is still bound is not inferred deaf from its unread inbox.
    # The waiting message shows as a pending count, where its sender reads it as unread.
    assert type(readiness(connected)) is ReadyDrainReadiness
    assert connected.bus.pending_count("bridge") == 1
    assert UnitDeaf.violations(connected) == ()
