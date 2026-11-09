"""Bind the actual original message modules instead of absent names on sending."""

import ast
import hashlib
import json
import subprocess
from pathlib import Path

repository = Path('/home/ts/wt/comms-friction--P17')
base = '356d83cc039ce74469a30a2817c8275fe90bf527'
root = repository / 'src/agent_comms'
old = 'from agent_comms.messaging.sending import messages'
new = 'import agent_comms.messaging.messages as messages'
reason = (
    'sending.py imports individual declarations from messaging.messages, but binds '
    'neither messages nor retained_task_facts as a module. Seven provider imports '
    'incorrectly require those absent attributes from sending, which also pulls '
    'Registration/Messaging/task assembly into their bootstrap. Bind the actual '
    'original messaging.messages and messaging.retained_task_facts modules directly. '
    'Keep each existing local module name, concrete record annotation, dataclass '
    'field, default, method body, constructor and acquisition/refusal/I/O sequence. '
    'No declaration movement, replacement record, re-export shim, guarded promotion '
    'or local import. Normal native reproof checks the integrated source; no runtime '
    'success or SCC reduction is claimed from this artifact.'
)
replacements = [
    (name, old, new) for name in (
        'bus_record_builder.py', 'messaging/initial_origin.py',
        'messaging/message_writer.py', 'messaging/retained_task_facts.py',
    )
] + [(
    'messaging/messages.py',
    'from agent_comms.messaging.sending import retained_task_facts as task_facts',
    'import agent_comms.messaging.retained_task_facts as task_facts',
), (
    'messaging/task_sources.py',
    'from agent_comms.messaging.sending import retained_task_facts as task_facts\n'
    'from agent_comms.messaging.sending import messages',
    'import agent_comms.messaging.retained_task_facts as task_facts\n'
    'import agent_comms.messaging.messages as messages',
)]
operations = []
checks = []
for name, before, after in replacements:
    source = subprocess.run(
        ['git', '-C', str(repository), 'show', f'{base}:src/agent_comms/{name}'],
        check=True, text=True, capture_output=True,
    ).stdout
    assert source.count(before) == 1, name
    projected = source.replace(before, after, 1)
    original_nodes = [ast.dump(n) for n in ast.parse(source).body
                      if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    projected_nodes = [ast.dump(n) for n in ast.parse(projected).body
                       if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    assert original_nodes == projected_nodes, name
    target = name
    operations.append({'operation': 'patch_target', 'file_path': str(root / target),
                       'replacements': [{'old_source': before, 'new_source': after}]})
    checks.append({'source': name, 'target': target,
                   'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                   'declarations_and_bodies_unchanged': True})
plan = {'stages': [{'recipes': [{
    'recipe_id': 'p23-bind-original-message-module-owners', 'reason': reason,
    'operations': operations, 'architecture_guards': [], 'authority_claims': [],
}], 'architecture_guards': []}]}
output = Path(__file__).parent
plan_path = output / 'message-module-owners.plan.json'
plan_path.write_text(json.dumps(plan, indent=2) + '\n')
(output / 'message-module-owners.source-check.json').write_text(json.dumps({
    'base': base, 'checks': checks,
    'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(),
    'scope': 'Source checks only; native application remains with friction',
}, indent=2) + '\n')
print(plan_path)
