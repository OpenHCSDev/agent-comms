"""doctrine-segment: offline carry proof; no provider/agent launch."""
from pathlib import Path
from dataclasses import replace
from contextlib import closing
import importlib.util
import json
import os
import sqlite3
from uuid import uuid4
from agent_comms.child_process import ProcessIdentity

from agent_comms.comms import Comms
from agent_comms.goals import Goal
from agent_comms.goal_states import BlockedGoal, CompletedGoal, PausedGoal, OwnerPause
from agent_comms.goal_actions import (
    GoalPrecondition, RetryGoalAction, ActiveGoalAction, OwnerInvocable,
    RuntimeInvocable, ModelInvocable,
)
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goal_attempts import MissingGoalGenerationError, GoalAttemptStore
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread

base = Path('/home/ts/wt/core-doctrine-segment/.scratch/existing-generation-smoke-v3')
base.mkdir(mode=0o700, exist_ok=False)
root = base / 'wire'
c = Comms(root)
store = c.goals.open_goal_store()
spec = importlib.util.spec_from_file_location('carry_once', Path.cwd() / 'tools/cutover/carry_existing_goal_generations.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
for name, state in [('blocked', BlockedGoal('Historical missing launch record')),
                    ('paused', PausedGoal(OwnerPause())), ('completed', CompletedGoal())]:
    goal = Goal(id=uuid4().hex, text='Legacy ' + name, state=state)
    c.registry.declare(Thread(name, frozenset({'test'}), str(base), goal=goal,
                              process_identity=ProcessIdentity.capture(os.getpid())))
# An existing attempted goal must not be rewritten.
goal = Goal(id=uuid4().hex, text='Existing attempt', state=BlockedGoal('Recorded failure'))
c.registry.declare(Thread('existing', frozenset({'test'}), str(base), goal=goal))
store.create_goal(goal.id)
a = store.reserve(goal.id, 1)
store.claim_launch(a)
a.fail(store, 'Known recorded failure')
existing = store.require_generation(goal.id)
blocked = c.registry.require('blocked')
try:
    store.require_generation(blocked.goal.id)
except MissingGoalGenerationError:
    pass
else:
    raise AssertionError('Missing historical generation must refuse before carry')
pre_registry = (root / 'registry.json').read_bytes()
unknown = InputDispositions(root / InputDispositions.filename)
unknown.record('acp:preserved-unknown', seq=None, owner='blocked',
               admission=c.registry.snapshot().admission_generations['blocked'],
               target='blocked', text='Do not replay this input')
pre_inputs = unknown.path.read_bytes()
preview = m.preview(root.resolve())
assert preview['missing_count'] == 3
result = m.carry(root.resolve(), base / 'backup')
assert result['carried_count'] == 3 and result['after']['missing_count'] == 0
assert (root / 'registry.json').read_bytes() == pre_registry
assert unknown.path.read_bytes() == pre_inputs
assert store.require_generation(goal.id) == existing
reopened = GoalAttemptStore(root / 'goal-private')
for name in ('blocked', 'paused', 'completed'):
    row = reopened.require_generation(c.registry.require(name).goal.id)
    assert row.number == 1 and row.lifecycle == ReadyGeneration() and row.attempt_id is None
# Retry remains explicit; fresh generation, same goal, no synthetic attempt.
expect = GoalPrecondition(goal_id=blocked.goal.id, expected_goal=blocked.goal,
    expected_owner=blocked.process_identity)
for actor in (RuntimeInvocable, ModelInvocable, OwnerInvocable):
    try:
        c.goals.update_goal('blocked', ActiveGoalAction(expect=expect), actor=actor)
    except ValueError:
        pass
    else:
        raise AssertionError('A carried blocked goal cannot Resume without explicit Retry')
    assert c.registry.require('blocked').goal == blocked.goal
    assert store.require_generation(blocked.goal.id).number == 1
retried = c.goals.update_goal('blocked', RetryGoalAction(expect=expect), actor=OwnerInvocable)
assert retried.id == blocked.goal.id and retried.state.active
assert store.require_generation(retried.id).number == 2
assert unknown.path.read_bytes() == pre_inputs
assert m.preview(root.resolve())['missing_count'] == 0
# Repeat with a fresh backup performs no ledger writes.
repeat = m.carry(root.resolve(), base / 'repeat-backup')
assert repeat['carried_count'] == 0
with closing(sqlite3.connect(base / 'backup/goal_attempts.sqlite3')) as conn:
    assert conn.execute('SELECT COUNT(*) FROM generation').fetchone()[0] == 1
print(json.dumps({'carried': 3, 'repeat': 0, 'explicit_retry_generation': 2,
                 'existing_attempt_untouched': True, 'public_goal_bytes_untouched': True,
                 'unknown_bytes_untouched': True, 'backup_before_carry': True}))
