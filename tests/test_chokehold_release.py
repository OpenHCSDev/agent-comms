"""The teammate a chokehold paused is found again, so its quoting answer can resume it."""
from pathlib import Path
from types import SimpleNamespace

from agent_comms.repo import Checkout
from functools import partial

from agent_comms.team.vibe_check import Chokehold, ChokeholdAnswer
from agent_comms.goal_actions import ChokeholdPausedGoalAction, OwnerInvocable, PausedGoalAction
from agent_comms.goal_states import ChokeholdPause, OwnerPause
from agent_comms.task_attachments.owner_notices import ChokeholdNotice
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool
from agent_comms.threads import Thread


def _teammate(comms, monkeypatch, name):
    monkeypatch.setenv("AGENT_COMMS_THREAD", name)
    comms.registry.declare(Thread(name=name, tags=frozenset(), worktree=Checkout(Path("/wt"))))
    partial(invoke_tool, caller=ThreadCaller(name))(comms, "comms_set_goal", {"text": "work"})


def test_only_the_teammate_a_chokehold_paused_is_held(comms, monkeypatch):
    _teammate(comms, monkeypatch, "choked")
    _teammate(comms, monkeypatch, "owner-paused")
    comms.goals.update_goal("choked", ChokeholdPausedGoalAction())
    comms.goals.update_goal("owner-paused", PausedGoalAction())

    assert ChokeholdAnswer.held(comms, "choked")
    assert not ChokeholdAnswer.held(comms, "owner-paused")


def test_a_chokehold_pauses_only_the_teammate_who_ignored_the_check(comms, monkeypatch):
    """Teammates sharing the board keep working; the one who ignored the vibe check stops."""
    _teammate(comms, monkeypatch, "ignored")
    _teammate(comms, monkeypatch, "running")
    burst = []
    monkeypatch.setattr(ChokeholdNotice, "burst_once", lambda *args: burst.append(args))

    Chokehold(SimpleNamespace(seq=1, message_id="check")).enforce(comms, "ignored")

    assert isinstance(comms.registry.require("ignored").goal.state.source, ChokeholdPause)
    assert not ChokeholdAnswer.held(comms, "running")
    assert burst


def test_a_chokehold_leaves_an_owner_pause_alone(comms, monkeypatch):
    """A goal the owner paused must not kill the heartbeat before the burst goes out."""
    _teammate(comms, monkeypatch, "owner-paused")
    comms.goals.update_goal("owner-paused", PausedGoalAction(), actor=OwnerInvocable)
    burst = []
    monkeypatch.setattr(ChokeholdNotice, "burst_once", lambda *args: burst.append(args))

    Chokehold(SimpleNamespace(seq=1, message_id="check")).enforce(comms, "owner-paused")

    assert isinstance(comms.registry.require("owner-paused").goal.state.source, OwnerPause)
    assert burst
