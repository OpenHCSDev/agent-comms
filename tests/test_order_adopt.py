"""An adopted goal takes a new problem without being rewritten, and every refusal writes nothing."""

from dataclasses import replace

import pytest

from agent_comms.errors import TodoError
from agent_comms.goal_actions import (
    AdoptGoalAction,
    CompletedGoalAction,
    CreateProblemGoalAction,
    GoalAdoption,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goals import AdoptedGoalOrder, GoalChangedError, GoalRevision
from agent_comms.order_tools import CommsTodoCreateTool
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.repo import Checkout
from agent_comms.repository_name import RepositoryName
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import PullRequestLink
from agent_comms.tool_caller import ThreadCaller
from source_revision_fixture import SourceRepository
from test_todos import order


@pytest.fixture
def adopting(comms, tmp_path, monkeypatch):
    """A worker whose own paused goal has progress, a lead commanding the bus, and GitHub
    reporting every pull request it is asked about."""
    monkeypatch.setattr(PullRequestLink, "_reported_number",
                        staticmethod(lambda repo, reference: int(reference)))
    source = SourceRepository.create(tmp_path / "source")
    for name in ("lead", "worker", "other", "rogue"):
        comms.registry.declare(Thread(name, frozenset({"comms"}), Checkout(tmp_path)))
    lead = comms.registry.require("lead").incarnation
    comms.registry.set_commander_project(RegisteredCommanderProject(lead, (source,)))
    comms.goals.update_goal("worker", SetGoalAction(text="Land the hook import cleanup."))
    comms.goals.update_goal("worker", PausedGoalAction(progress="Hook imports moved; tests next."))
    return source, comms.goals.todos.add_request(lead, "Order the hook import cleanup").id


def adoption_of(comms, source, request, *, problem="P1", caller="lead",
                unit=None, goal=None, link=None, repo=None):
    worker = comms.registry.require("worker")
    current = worker.goal.require()
    return ThreadCaller(caller).invoke(CommsTodoCreateTool(owner="worker", order=AdoptGoalAction(
        problem=problem, order=order(source).carrying((link or source.link,)),
        request=request, review_at=RecordedEventTime(1000),
        commander=caller, assigner=comms.registry.require(caller).incarnation,
        repo=repo or source.github, text="Land the hook import cleanup",
        adoption=GoalAdoption(unit or worker.incarnation, goal or current.revision_key))), comms)


def test_adopt_keeps_the_goal_and_its_todo_decides_completion(comms, adopting):
    source, request = adopting
    before = comms.registry.require("worker").goal.require()
    attempts = comms.goals.open_goal_store().snapshot(before.id)
    waits = comms.goals.waits.read()

    adoption_of(comms, source, request)

    after = comms.registry.require("worker").goal.require()
    assert after == replace(before, order=AdoptedGoalOrder())
    assert comms.goals.open_goal_store().snapshot(before.id) == attempts
    assert comms.goals.waits.read() == waits
    todo = comms.goals.todos.get("P1")
    assert todo.held().generation == before.id
    assert todo.order.pull_requests == (source.link,)
    worker = comms.registry.require("worker").incarnation
    instructions, = after.instructions_for(comms.goals, worker)
    assert instructions.startswith("Land the hook import cleanup.\n\nProblem: P1:")
    assert after.wake_context(comms.goals, worker).progress.startswith(
        "Hook imports moved; tests next.\n")
    with pytest.raises(ValueError, match="Deliver each assigned problem"):
        comms.goals.update_goal("worker", CompletedGoalAction())
    with pytest.raises(ValueError, match="already holds an open order"):
        adoption_of(comms, source, request, problem="P2",
                    link=PullRequestLink(source.github, source.number + 1))
    assert comms.registry.require("worker").goal.require() == after


UNREGISTERED = RepositoryName.from_wire("org/unregistered")


def stale(comms, source, request, worker):
    goal = worker.goal.require()
    return {"goal": GoalRevision(goal.id, goal.revision - 1)}


def reborn(comms, source, request, worker):
    return {"unit": RecordedThreadIncarnation("worker", worker.incarnation.wire_birth + 1)}


def rogue(comms, source, request, worker):
    return {"caller": "rogue"}


def foreign(comms, source, request, worker):
    return {"link": PullRequestLink(UNREGISTERED, source.number)}


def unregistered(comms, source, request, worker):
    return {"link": PullRequestLink(UNREGISTERED, source.number), "repo": UNREGISTERED}


def held(comms, source, request, worker):
    comms.goals.update_goal("other", SetGoalAction(text="Other work."))
    ThreadCaller("lead").invoke(CommsTodoCreateTool(owner="other", order=CreateProblemGoalAction(
        problem="P9", order=order(source).carrying((source.link,)), request=request,
        review_at=RecordedEventTime(1000), commander="lead",
        assigner=comms.registry.require("lead").incarnation, repo=source.github,
        text="Other work on the same pull request")), comms)
    return {}


@pytest.mark.parametrize(("refusal", "error", "message"), (
    (stale, GoalChangedError, "the action was not applied"),
    (reborn, ValueError, "no longer the thread born"),
    (rogue, ValueError, "not this bus's commander"),
    (foreign, ValueError, "not in the order's registered repository"),
    (unregistered, ValueError, "not in the order's registered repository"),
    (held, ValueError, "already belongs to open problem P9"),
))
def test_adopt_refusal_writes_nothing(comms, adopting, refusal, error, message):
    source, request = adopting
    worker = comms.registry.require("worker")
    goal = worker.goal.require()
    attempts = comms.goals.open_goal_store().snapshot(goal.id)
    with pytest.raises(error, match=message):
        adoption_of(comms, source, request, **refusal(comms, source, request, worker))
    with pytest.raises(TodoError, match="Unknown todo: P1"):
        comms.goals.todos.get("P1")
    assert comms.registry.require("worker").goal.require() == goal
    assert comms.goals.open_goal_store().snapshot(goal.id) == attempts
