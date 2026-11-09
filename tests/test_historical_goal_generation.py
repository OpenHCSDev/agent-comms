"""A fresh carry generation says nothing about the old goal's execution."""

import os
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_key import AcpInputKey
from agent_comms.input_disposition import InputDispositions
from agent_comms.nominal.field_codec import GoalId, InputId
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    RetryGoalAction,
    RuntimeInvocable,
)
from agent_comms.planning.goal_attempts import GoalAttemptStore
from agent_comms.planning.goal_generation import ReadyGeneration
from agent_comms.planning.goal_ledger import MissingGoalGenerationError
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.planning.goal_states import BlockedGoal
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile

UNKNOWN_ORIGINAL_KEY = AcpInputKey(InputId.mint())


def test_carried_blocked_goal_requires_explicit_retry_preserving_unknown(tmp_path):
    comms = wire(tmp_path / 'wire')
    store = comms.goals.open_goal_store()
    goal_id = GoalId.mint()
    goal = Goal(published_seq=0, text='Historical objective; past execution is uncertain',
        id=goal_id,
                state=BlockedGoal('Inspect uncertain original work before explicit Retry'))
    thread = comms.registry.declare(NativeThread('legacy', frozenset({'test'}), Checkout(tmp_path), goal=goal, process_identity=ProcessIdentity.capture(os.getpid())))
    inputs = InputDispositions(comms.root)
    inputs.record(UNKNOWN_ORIGINAL_KEY, seq=None, owner=thread.name,
                  claim_scope=comms.registry.snapshot().claim_generation(thread.name),
                  target=thread.name, text='This uncertain original must not replay')
    original_inputs = inputs.path.read_bytes()
    original_registry = comms.registry.store.rows.mark()
    with pytest.raises(MissingGoalGenerationError):
        store.require_generation(goal.id)

    # The one-time tool calls this existing writer after backup and absence
    # checks. This is a new unreserved generation, not evidence of no past run.
    store.create_goal(goal.id)
    assert comms.registry.store.rows.mark() == original_registry
    reopened = GoalAttemptStore.at(comms.root / 'goal-private')
    assert reopened.require_generation(goal.id).lifecycle == ReadyGeneration()
    expect = GoalPrecondition(goal_id=OneGoal(goal.id), expected_checkpoint=goal.captured,
                              expected_owner=thread.process_identity)
    for actor in (ModelInvocable, RuntimeInvocable, OwnerInvocable):
        with pytest.raises(ValueError):
            comms.goals.update_goal(thread.name, ActiveGoalAction(expect=expect), actor=actor)
        assert comms.registry.require(thread.name).goal == goal
        assert store.require_generation(goal.id).number == 1

    with pytest.raises(ValueError):
        stale = replace(expect, expected_owner=replace(
            thread.process_identity, start_time=thread.process_identity.start_time + 1))
        comms.goals.update_goal(thread.name, RetryGoalAction(expect=stale), actor=OwnerInvocable)
    assert store.require_generation(goal.id).number == 1
    retried = comms.goals.update_goal(thread.name, RetryGoalAction(expect=expect), actor=OwnerInvocable)
    retried.require_active()
    assert retried.id == goal.id
    generation = store.require_generation(goal.id)
    assert generation.number == 2 and generation.attempt_id.present() == ()
    assert inputs.path.read_bytes() == original_inputs
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempt_record').fetchone()[0] == 0
