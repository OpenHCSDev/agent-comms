"""A unit the chokehold paused is found again, so a quoting answer can resume it."""
from pathlib import Path
from types import SimpleNamespace

from agent_comms.repo import Checkout
from functools import partial

from agent_comms.commander.vibe_check import Chokehold, ChokeholdAnswer
from agent_comms.goal_actions import ChokeholdPausedGoalAction, OwnerInvocable, PausedGoalAction
from agent_comms.goal_states import ChokeholdPause, OwnerPause
from agent_comms.task_attachments.owner_notices import ChokeholdNotice
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool
from agent_comms.threads import Thread


def _unit(comms, monkeypatch, name):
    monkeypatch.setenv("AGENT_COMMS_THREAD", name)
    comms.registry.declare(Thread(name=name, tags=frozenset(), worktree=Checkout(Path("/wt"))))
    partial(invoke_tool, caller=ThreadCaller(name))(comms, "comms_set_goal", {"text": "work"})


def test_a_chokehold_paused_unit_is_held_and_an_owner_pause_is_not(comms, monkeypatch):
    _unit(comms, monkeypatch, "choked")
    _unit(comms, monkeypatch, "owner-paused")
    _unit(comms, monkeypatch, "running")
    comms.goals.update_goal("choked", ChokeholdPausedGoalAction())
    comms.goals.update_goal("owner-paused", PausedGoalAction())

    assert ChokeholdAnswer.held(comms) == ("choked",)


def test_a_chokehold_holds_the_others_and_leaves_an_owner_pause_alone(comms, monkeypatch):
    """One unit the owner paused must not kill the heartbeat before the burst goes out."""
    _unit(comms, monkeypatch, "owner-paused")
    _unit(comms, monkeypatch, "running")
    comms.goals.update_goal("owner-paused", PausedGoalAction(), actor=OwnerInvocable)
    burst = []
    monkeypatch.setattr(ChokeholdNotice, "burst_once", lambda *args: burst.append(args))

    units = tuple(comms.registry.require(name).incarnation for name in ("owner-paused", "running"))
    Chokehold(SimpleNamespace(seq=1, message_id="check"), units).enforce(comms, "owner-paused")

    source = {name: comms.registry.require(name).goal.state.source for name in ("owner-paused", "running")}
    assert isinstance(source["owner-paused"], OwnerPause)
    assert isinstance(source["running"], ChokeholdPause)
    assert burst
