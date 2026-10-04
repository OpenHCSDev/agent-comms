"""Source receipt through existing NRA package parser; no dynamic-resolution claim."""
import ast
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/home/ts/.codex/skills/refactor-audit/scripts')
from audit.findings import Package
from audit.repository import Repository
repo = Repository(Path.cwd())
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
roots = ['src/agent_comms', 'tests', 'tools']
packages = [Package.load(repo, head, root) for root in roots]
names = {'WireLog', 'InputDrain', 'WireWatch', 'StoreLock', 'StoreLockContention', 'Coordination', 'CoordinationStore', 'certified_read', '_async_store_lock', '_store_lock', '_private_observation_revision', '_drain_private_if_changed', 'drain_owned_inbox', 'observe', 'run_async', 'try_store_lock', 'acquire_async', 'read_certified_async', '_read_certified', '_certified_source', '_observe_private_revision'}
declarations, consumers = [], []
for package in packages:
    for module in package.modules:
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                declarations.append({'path': module.path, 'line': node.lineno, 'name': node.name,
                    'bases': [ast.unparse(base) for base in getattr(node, 'bases', [])],
                    'source': ast.unparse(node)})
            if isinstance(node, ast.Name) and node.id in names:
                consumers.append({'path': module.path, 'line': node.lineno, 'kind': type(node.ctx).__name__, 'source': ast.unparse(node)})
            elif isinstance(node, ast.Attribute) and node.attr in names:
                consumers.append({'path': module.path, 'line': node.lineno, 'kind': type(node.ctx).__name__, 'source': ast.unparse(node)})
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(alias.name.rsplit('.', 1)[-1] in names for alias in node.names):
                consumers.append({'path': module.path, 'line': node.lineno, 'kind': 'Import', 'source': ast.unparse(node)})
receipt = {'head': head, 'parser': 'existing refactor-audit Package.load Python3.14',
    'roots': roots, 'modules': sum(len(p.modules) for p in packages),
    'parse_omissions': [path for p in packages for path in p.unparsed],
    'names': sorted(names), 'declarations': declarations, 'candidate_consumers': consumers,
    'limits': 'Attribute/callback candidates require semantic reading; AST cannot prove dynamic dispatch. Four new WireLog/InputDrain methods explicitly included in after names.'}
assert not receipt['parse_omissions']
Path('evidence/idle-drain-source-custody-20261003/after-ast.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({'modules': receipt['modules'], 'omissions': receipt['parse_omissions'], 'declarations': len(declarations), 'candidate_consumers': len(consumers)}))
