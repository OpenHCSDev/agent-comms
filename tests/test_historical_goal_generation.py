"""A fresh carry generation says nothing about the old goal's execution."""
from contextlib import closing
from dataclasses import replace
import os
import sqlite3
from uuid import uuid4

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import (
    ActiveGoalAction, GoalPrecondition, ModelInvocable, OwnerInvocable,
    RetryGoalAction, RuntimeInvocable,
)
from agent_comms.goal_attempts import GoalAttemptStore, MissingGoalGenerationError
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread


def test_carried_blocked_goal_requires_explicit_retry_preserving_unknown(tmp_path):
    comms = Comms(tmp_path / 'wire')
    store = comms.goals.open_goal_store()
    goal = Goal(published_seq=0, text='Historical objective; past execution is uncertain', id=uuid4().hex,
                state=BlockedGoal('Inspect uncertain original work before explicit Retry'))
    thread = comms.registry.declare(comms.registry.new_thread('legacy', frozenset({'test'}), str(tmp_path), goal=goal, process_identity=ProcessIdentity.capture(os.getpid())))
    inputs = InputDispositions(comms.root)
    inputs.record('acp:unknown-original', seq=None, owner=thread.name,
                  claim_scope=comms.registry.snapshot().claimed_generations[thread.name],
                  target=thread.name, text='This uncertain original must not replay')
    original_inputs = inputs.path.read_bytes()
    original_registry = (comms.root / 'registry.json').read_bytes()
    with pytest.raises(MissingGoalGenerationError):
        store.require_generation(goal.id)

    # The one-time tool calls this existing writer after backup and absence
    # checks. This is a new unreserved generation, not evidence of no past run.
    store.create_goal(goal.id)
    assert (comms.root / 'registry.json').read_bytes() == original_registry
    reopened = GoalAttemptStore(comms.root / 'goal-private')
    assert reopened.require_generation(goal.id).lifecycle == ReadyGeneration()
    expect = GoalPrecondition(goal_id=goal.id, expected_goal=goal,
                              expected_owner=thread.process_identity)
    for actor in (ModelInvocable, RuntimeInvocable, OwnerInvocable):
        with pytest.raises(ValueError, match='explicit retry'):
            comms.goals.update_goal(thread.name, ActiveGoalAction(expect=expect), actor=actor)
        assert comms.registry.require(thread.name).goal == goal
        assert store.require_generation(goal.id).number == 1

    with pytest.raises(ValueError, match='owner changed'):
        stale = replace(expect, expected_owner=replace(
            thread.process_identity, start_time=thread.process_identity.start_time + 1))
        comms.goals.update_goal(thread.name, RetryGoalAction(expect=stale), actor=OwnerInvocable)
    assert store.require_generation(goal.id).number == 1
    retried = comms.goals.update_goal(thread.name, RetryGoalAction(expect=expect), actor=OwnerInvocable)
    assert retried.id == goal.id and retried.state.active
    generation = store.require_generation(goal.id)
    assert generation.number == 2 and generation.attempt_id is None
    assert inputs.path.read_bytes() == original_inputs
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempt_record').fetchone()[0] == 0
