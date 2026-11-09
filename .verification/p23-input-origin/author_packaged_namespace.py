"""Pin the two ingress namespace corrections to friction's current-main checkpoint."""

import ast
import hashlib
import json
import subprocess
from pathlib import Path

REPOSITORY = Path('/home/ts/wt/comms-friction--P17')
COMMIT = '1c532ae3a83094b4a59438e0500a2692e33ab128'
OUTPUT = Path(__file__).parent
CORRECTIONS = (
    ('input_disposition.py', 'import agent_comms.bus as bus',
     'import agent_comms.messaging.bus as bus',
     'messaging/bus.py'),
    ('execution/input_origin.py', 'import agent_comms.retained_task_facts as task_facts',
     'import agent_comms.messaging.retained_task_facts as task_facts',
     'messaging/retained_task_facts.py'),
)
operations = []
evidence = []
for relative, before, after, provider in CORRECTIONS:
    source = subprocess.run(
        ['git', '-C', str(REPOSITORY), 'show', f'{COMMIT}:src/agent_comms/{relative}'],
        check=True, capture_output=True, text=True,
    ).stdout
    subprocess.run(['git', '-C', str(REPOSITORY), 'cat-file', '-e',
                    f'{COMMIT}:src/agent_comms/{provider}'], check=True)
    assert source.count(before) == 1, relative
    projected = source.replace(before, after, 1)
    old_classes = [ast.dump(node) for node in ast.parse(source).body if isinstance(node, ast.ClassDef)]
    new_classes = [ast.dump(node) for node in ast.parse(projected).body if isinstance(node, ast.ClassDef)]
    assert old_classes == new_classes, relative
    operations.append({
        'operation': 'patch_target',
        'file_path': str(REPOSITORY / 'src/agent_comms' / relative),
        'replacements': [{'old_source': before, 'new_source': after}],
    })
    evidence.append({'file': relative, 'canonical_provider': provider,
                     'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                     'classes_and_methods_unchanged': True})
reason = (
    'Current-main replay 1c532ae3a retains two imports naming removed root modules. '
    'Restore the existing packaged module owners, keeping the same local aliases '
    'and all original class declarations, fields and method bodies. Bus remains '
    'the actual messaging.bus.Bus instantiated inside harness_attempts; retained '
    'facts remain the actual messaging.retained_task_facts declarations. These '
    'imports are already unconditional: this is namespace repair, not guarded/local '
    'import promotion, declaration movement, constructor timing change or new layer. '
    'Apply within friction\'s coherent component batch and retain its combined '
    'cycle/cold/MRO/consumer qualification. No runtime success is claimed here.'
)
plan = {'stages': [{'recipes': [{'recipe_id': 'p23-packaged-ingress-module-owners',
                               'reason': reason, 'operations': operations,
                               'architecture_guards': [], 'authority_claims': []}],
                    'architecture_guards': []}]}
plan_path = OUTPUT / 'packaged-ingress-namespace.plan.json'
plan_path.write_text(json.dumps(plan, indent=2) + '\n')
receipt = {'base': COMMIT, 'checks': evidence,
           'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(),
           'scope': 'Pinned-source ownership/AST checks only; no native apply or cold run'}
(OUTPUT / 'packaged-ingress-namespace.receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
