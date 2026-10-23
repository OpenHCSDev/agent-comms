"""A unit the chokehold paused is found again, so a quoting answer can resume it."""

from functools import partial

from agent_comms.commander.vibe_check import ChokeholdAnswer
from agent_comms.goal_actions import ChokeholdPausedGoalAction, PausedGoalAction
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool
from agent_comms.threads import Thread


def _unit(comms, monkeypatch, name):
    monkeypatch.setenv("AGENT_COMMS_THREAD", name)
    comms.registry.declare(Thread(name=name, tags=frozenset(), worktree="/wt"))
    partial(invoke_tool, caller=ThreadCaller(name))(comms, "comms_set_goal", {"text": "work"})


def test_a_chokehold_paused_unit_is_held_and_an_owner_pause_is_not(comms, monkeypatch):
    _unit(comms, monkeypatch, "choked")
    _unit(comms, monkeypatch, "owner-paused")
    _unit(comms, monkeypatch, "running")
    comms.goals.update_goal("choked", ChokeholdPausedGoalAction())
    comms.goals.update_goal("owner-paused", PausedGoalAction())

    assert ChokeholdAnswer.held(comms) == ("choked",)
