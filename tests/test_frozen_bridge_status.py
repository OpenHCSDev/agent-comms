"""A frozen session bridge stops taking its inbox; that, not its live process, is what it reads.

On 2026-11-04 a blocking py-spy paused a session bridge for almost four minutes. Its process was
alive, so it read `connected` the whole time while its messages waited. These tests freeze a real
registered process with SIGSTOP and read every surface that showed `connected`.
"""

import os
import signal
import subprocess
from dataclasses import replace
from collections.abc import Iterator

import pytest

from agent_comms.cli_bridges.claude_transcript import ReconnectUrl
from agent_comms.cli_bridges.harness_delivery_states import TakingLedger
from agent_comms.cli_bridges.inbox_watch import InboxWatch
from agent_comms.comms import InboxActivity, wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.activity import (
    ActivityState,
    ReadyDrainReadiness,
    StalledDeliveryReadiness,
)
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


NO_RECONNECT_ASKED = ReconnectUrl(url="ws://127.0.0.1/watch/unused")
"""These tests take and read the inbox only; no client here re-dials, so its URL is never sent."""


def sent_to_bridge(comms, text):
    return comms.messaging.send("sender", "bridge", text)


def take(tmp_path, message) -> None:
    """The harness delivery a reader records when it takes a message: the connector's own."""
    InboxWatch("bridge", tmp_path, NO_RECONNECT_ASKED).taken(message, TakingLedger())


def untaken(tmp_path):
    return InboxWatch("bridge", tmp_path, NO_RECONNECT_ASKED).untaken()


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
    # the sidebar roster reads it inside the bus snapshot it already holds
    row = wire(tmp_path).views.presentation.roster("sender").threads["bridge"]
    assert "not reading since" in " ".join(row.presentation.status.details)
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


def test_one_reader_sees_a_take_clear_the_stall_with_no_new_message(
        tmp_path, connected, bridge_process, instant_stall):
    """One observer keeps its InboxActivity across passes; a take writes coordination and
    no bus message, and that alone must re-read the stall."""
    observer = wire(tmp_path)

    def observed():
        return observer.agents.activity_of("bridge", snapshot=observer.registry.snapshot()).readiness

    os.kill(bridge_process.pid, signal.SIGSTOP)
    sent_to_bridge(connected, "probe while frozen")
    assert type(observed()) is StalledDeliveryReadiness
    assert type(observed()) is StalledDeliveryReadiness

    os.kill(bridge_process.pid, signal.SIGCONT)
    for message in untaken(tmp_path):
        take(tmp_path, message)

    assert type(observed()) is ReadyDrainReadiness



def test_captures_at_one_cut_share_one_inbox_read(tmp_path, connected):
    """One observer pass shares its oldest-message answer until determining inputs change."""
    observer = wire(tmp_path)

    def capture():
        observer.agents.activity_of("bridge", snapshot=observer.registry.snapshot())
        return observer.agents.untaken

    first = capture()
    assert capture() is first
    sent_to_bridge(connected, "a new message is a new cut")
    first = capture()
    assert first.oldest.keys() == {"bridge"}
    assert capture() is first

    # Goal writes share the coordinator, but do not consume this inbox's messages.
    connected.goals.update_goal("sender", SetGoalAction(text="Unrelated work"))
    assert capture() is first

    # A presentation edit decodes a new registry object without changing routing.
    connected.registry.update_thread("sender", lambda thread: replace(thread, title=GivenThreadText("A new title")))
    assert capture() is first


def test_taking_the_minimum_keeps_other_unread_messages(tmp_path, connected):
    observer = wire(tmp_path)
    first = sent_to_bridge(connected, "first unread").message
    assert observer.agents.oldest_untaken(observer.registry.snapshot()) == {
        "bridge": first.timestamp,
    }
    sent_to_bridge(connected, "later unread")
    assert observer.agents.oldest_untaken(observer.registry.snapshot()) == {
        "bridge": first.timestamp,
    }
    take(tmp_path, first)
    current = observer.agents.oldest_untaken(observer.registry.snapshot())
    assert current["bridge"] > first.timestamp
