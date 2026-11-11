"""The teammate a chokehold paused is found again, so its quoting answer can resume it."""
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from agent_comms.planning.goal_actions import (
    ChokeholdPausedGoalAction,
    OwnerInvocable,
    PausedGoalAction,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.task_attachments.owner_notices import ChokeholdNotice
from agent_comms.team.vibe_check import Chokehold, ChokeholdAnswer
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.served import invoke_tool


def _teammate(comms, monkeypatch, name):
    monkeypatch.setenv("AGENT_COMMS_THREAD", name)
    comms.registry.declare(NativeThread(name=name, tags=frozenset(), worktree=Checkout(Path("/wt"))))
    partial(invoke_tool, caller=ThreadCaller(name))(comms, "comms_set_goal", {"text": "work"})


def _burst(comms, teammate):
    with comms.bus.bus.reading() as source:
        return ChokeholdNotice.lines(source, comms.registry.snapshot(), teammate, time.time())


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
    Chokehold(comms.registry.require("ignored").incarnation,
              SimpleNamespace(seq=1, message_id="check")).enforce(comms)

    assert ChokeholdAnswer.held(comms, "ignored")
    assert not ChokeholdAnswer.held(comms, "running")
    assert _burst(comms, "ignored")


def test_a_chokehold_never_pauses_a_thread_that_replaced_its_teammate(comms, monkeypatch):
    """Decided for A's first birth; A is replaced under the same name before enforcement. The
    replacement is neither paused nor told, and the refusal is a declared one the heartbeat
    records as not applied."""
    import pytest

    from agent_comms.errors import BirthChangedError

    _teammate(comms, monkeypatch, "a")
    first = comms.registry.require("a")
    rule = Chokehold(first.incarnation, SimpleNamespace(seq=1, message_id="check"))
    comms.registry.unregister("a")
    comms.registry.remove("a")
    comms.registry.declare(NativeThread("a", first.tags, first.worktree, created_at=first.created_at + 1))
    partial(invoke_tool, caller=ThreadCaller("a"))(comms, "comms_set_goal", {"text": "work"})

    with pytest.raises(BirthChangedError):
        rule.enforce(comms)
    assert not ChokeholdAnswer.held(comms, "a")
    assert not _burst(comms, "a")


def test_a_chokehold_leaves_an_owner_pause_alone(comms, monkeypatch):
    """A goal the owner paused must not kill the heartbeat before the burst goes out."""
    _teammate(comms, monkeypatch, "owner-paused")
    comms.goals.update_goal("owner-paused", PausedGoalAction(), actor=OwnerInvocable)
    Chokehold(comms.registry.require("owner-paused").incarnation,
              SimpleNamespace(seq=1, message_id="check")).enforce(comms)

    assert not ChokeholdAnswer.held(comms, "owner-paused")
    assert _burst(comms, "owner-paused")


def test_any_teammate_can_pull_the_emergency_release(comms, monkeypatch):
    """The andon cord: an ordinary teammate's pull resumes the goals a chokehold paused,
    leaves an owner pause alone, and posts the pull under its own name."""
    from agent_comms.planning.goal_states import (
    ActiveGoal,
    OwnerPause,
)

    _teammate(comms, monkeypatch, "choked")
    _teammate(comms, monkeypatch, "owner-paused")
    _teammate(comms, monkeypatch, "puller")
    comms.goals.update_goal("choked", ChokeholdPausedGoalAction())
    comms.goals.update_goal("owner-paused", PausedGoalAction(), actor=OwnerInvocable)

    released = invoke_tool(comms, "comms_emergency_release", {}, caller=ThreadCaller("puller"))

    assert [identity["name"] for identity in released["release"]["released"]] == ["choked"]
    assert type(comms.registry.require("choked").goal.require().state) is ActiveGoal
    assert type(comms.registry.require("owner-paused").goal.require().state.pause_source) is OwnerPause
