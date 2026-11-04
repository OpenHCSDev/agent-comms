"""A frozen session bridge stops taking its inbox; that, not its live process, is what it reads.

On 2026-11-04 a blocking py-spy paused a session bridge for almost four minutes. Its process was
alive, so it read `connected` the whole time while its messages waited. These tests freeze a real
registered process with SIGSTOP and read every surface that showed `connected`.
"""

import os
import signal
import subprocess
from collections.abc import Iterator

import pytest

from agent_comms.activity import ActivityState, ReadyDrainReadiness, StalledDeliveryReadiness
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import InboxActivity, wire
from agent_comms.inbox_watch import InboxWatch
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
    """Thread `bridge` bound to the real process, as a connected CLI inbox; `sender` writes."""
    for name in ("bridge", "sender"):
        cli(tmp_path, "register", "--name", name, "--worktree", "/wt")
    with wire(tmp_path).registry.external_process(
            "bridge", ProcessIdentity.capture(bridge_process.pid)):
        yield wire(tmp_path)


def readiness(tmp_path):
    comms = wire(tmp_path)
    return comms.agents.activity_of("bridge", snapshot=comms.registry.snapshot()).readiness


def sent_to_bridge(comms, text):
    return comms.messaging.send("sender", "bridge", text)


def take(tmp_path, message) -> None:
    """The harness delivery a reader records when it takes a message: the connector's own."""
    InboxWatch("bridge", tmp_path).taken(message)


def untaken(tmp_path):
    return InboxWatch("bridge", tmp_path).untaken()


@pytest.fixture
def instant_stall(monkeypatch):
    """The window is a duration; the evidence is the untaken message, so test it at zero."""
    monkeypatch.setattr(InboxActivity, "stall_seconds", 0.0)
    monkeypatch.setattr(UnitDeaf, "seconds", 0)


def test_a_frozen_bridge_reads_stalled_everywhere_it_read_connected(
        cli, tmp_path, connected, bridge_process, instant_stall):  # noqa: F811
    os.kill(bridge_process.pid, signal.SIGSTOP)
    sent_to_bridge(connected, "probe while frozen")

    # The process is alive and the registry still says connected: that was the lie.
    assert cli(tmp_path, "thread", "--name", "bridge")[1]["status"] == "connected"
    stalled = readiness(tmp_path)
    assert type(stalled) is StalledDeliveryReadiness
    # comms_threads
    detail = cli(tmp_path, "thread", "--name", "bridge")[1]["activity_detail"]
    assert "not reading since" in detail
    # the sidebar row
    view, = (view for view in wire(tmp_path).views.thread_views() if view.thread.name == "bridge")
    assert "not reading since" in " ".join(view.presentation.status.details)
    # the board
    bridge = wire(tmp_path).registry.require("bridge").incarnation.require_recorded()
    assert [rule.unit for rule in UnitDeaf.violations(wire(tmp_path))] == [bridge]


def test_delivery_resuming_clears_the_stall(
        tmp_path, connected, bridge_process, instant_stall):
    os.kill(bridge_process.pid, signal.SIGSTOP)
    sent_to_bridge(connected, "probe while frozen")
    assert type(readiness(tmp_path)) is StalledDeliveryReadiness

    os.kill(bridge_process.pid, signal.SIGCONT)
    for message in untaken(tmp_path):
        take(tmp_path, message)

    assert type(readiness(tmp_path)) is ReadyDrainReadiness
    assert UnitDeaf.violations(wire(tmp_path)) == ()


def test_a_busy_bridge_mid_turn_is_not_frozen(tmp_path, connected, instant_stall):
    """A session working through a long turn still takes every message: the bridge records its
    attempt before the Monitor shows it, whatever the turn is doing."""
    connected.agents.set_activity("bridge", ActivityState.WORKING, "a long tool call")
    for number in range(3):
        sent_to_bridge(connected, f"message {number} during the turn")
        for message in untaken(tmp_path):
            take(tmp_path, message)

    assert type(readiness(tmp_path)) is ReadyDrainReadiness
    assert UnitDeaf.violations(wire(tmp_path)) == ()


def test_a_message_younger_than_the_window_is_not_a_stall(tmp_path, connected, bridge_process):
    os.kill(bridge_process.pid, signal.SIGSTOP)
    sent_to_bridge(connected, "just sent")

    assert type(readiness(tmp_path)) is ReadyDrainReadiness
