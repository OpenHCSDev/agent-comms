"""Exact-wheel CLI proof on a copy of the passing P51 saved root; never live state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import agent_comms
from agent_comms.goal_attempts import GoalAttemptStore, assert_goal_attempt_schema
from agent_comms.order_ledger import OrderRecords

base = Path('/home/ts/.cache/agent-scratch/core-commander-2/p107-runtime')
installed = Path(agent_comms.__file__).parent
assert base / 'env' in installed.parents, installed
exported = base / 'export/src/agent_comms'
for source in exported.rglob('*.py'):
    target = installed / source.relative_to(exported)
    assert target.read_bytes() == source.read_bytes(), target
assert issubclass(GoalAttemptStore, OrderRecords)
source = Path('/home/ts/.cache/agent-scratch/core-commander-2/p107-934-stream-final/test_problem_closure_and_goal_0/comms')
assert source.is_dir(), source
root = base / 'copied-root-final'
assert not root.exists(), 'Never replay a copied-root attempt.'
shutil.copytree(source, root, symlinks=True)

# Use the existing reviewed declaration carry, exclusively on this copy.
sys.path.insert(0, str(base / 'export/tools/cutover'))
from native_schema_carry import NativeSchemaDeclaration, carry_goal
from types import SimpleNamespace
schema = SimpleNamespace(goal=NativeSchemaDeclaration.observe_goal(),
                         require_goal=assert_goal_attempt_schema)
ledger = root / 'goal-private/goal_attempts.sqlite3'
with sqlite3.connect(ledger, isolation_level=None) as db:
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN IMMEDIATE')
    carry = carry_goal(db, schema)
    db.commit()
    db.execute('PRAGMA foreign_keys=ON')
    schema.require_goal(db)
(base / 'goal-carry.json').write_text(json.dumps(carry, indent=2) + '\n')

clean = {key: value for key, value in os.environ.items()
         if not key.startswith(('AGENT_COMMS_', 'PI_SESSION_', 'OPENCODE_SERVER_', 'PYTHON'))}
results = {}
for name, arguments in (
    ('comms_strike', dict(unit='worker-a', goal_id='order-a', struck='worker-a',
                         cause='false_done', caught='live',
                         what_happened='This copied-root check records a deliberate test strike without changing the original run.')),
    ('comms_order_story', dict(unit='worker-a', goal_id='order-a')),
):
    command = [sys.executable, '-I', '-m', 'agent_comms.cli', '--root', str(root),
               'invoke', '--from', 'lead', '--tool', name, '--arguments', json.dumps(arguments)]
    process = subprocess.run(command, env=clean, capture_output=True, text=True, timeout=45)
    (base / (name + '.stdout')).write_text(process.stdout)
    (base / (name + '.stderr')).write_text(process.stderr)
    assert process.returncode == 0, (name, process.returncode, process.stderr, process.stdout)
    results[name] = json.loads(process.stdout)
    (base / (name + '.json')).write_text(json.dumps(results[name], indent=2) + '\n')

strike, story = results['comms_strike'], results['comms_order_story']
assert strike['strikes'] == 1 and strike['fired'] is False, strike
assert strike['row']['id'] == story['strikes'][0]['id'], (strike, story)
assert len(story['strikes']) == 1
assert not (root / 'order_records.sqlite3').exists()
receipt = dict(head='af4b0d64f26b307ec08825cb4d47fdb1fd86340f',
               installed_package=str(installed), copied_from=str(source), copied_root=str(root),
               original_goal_rows=carry['original_rows'], carried_goal_rows=carry['carried_rows'],
               strike_count=strike['strikes'], strike_id=strike['row']['id'],
               story_strikes=len(story['strikes']), history_entries=len(story['history']),
               provider_calls=0, live_root_used=False)
(base / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
