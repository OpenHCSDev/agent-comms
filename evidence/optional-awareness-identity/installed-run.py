import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

repo = Path('/home/ts/wt/comms-selected-cold-compaction-native-20260928')
evidence = repo / 'evidence/optional-awareness-identity'
candidate = repo / '.artifacts/optional-awareness-identity/installed'
commit = '6cc7fd4cf96068565db0b576b1ae1f396980e361'
files = subprocess.check_output(['git', 'diff', '--name-only', 'd4f09edf', commit, '--', 'src/agent_comms'], cwd=repo, text=True).splitlines()
changed = {}
for relative in files:
    name = relative.removeprefix('src/').removesuffix('.py').replace('/', '.')
    module = importlib.import_module(name)
    installed = Path(module.__file__).resolve()
    assert installed.is_relative_to(candidate), (name, installed)
    assert installed.read_bytes() == subprocess.check_output(['git', 'show', f'{commit}:{relative}'], cwd=repo), name
    changed[name] = str(installed)
direct = json.loads(importlib.metadata.distribution('agent-comms').read_text('direct_url.json'))
assert not direct.get('dir_info', {}).get('editable', False)
os.environ.pop('PYTHONPATH', None)
receipt = {'commit': commit, 'python': sys.executable, 'direct_url': direct, 'changed_helpers': changed, 'boundary': 'noneditable wheel with actual persisted wire and SQLite awareness lifecycle; no provider/native/UI claim'}
(evidence/'installed-imports.json').write_text(json.dumps(receipt, indent=2)+'\n')
print(json.dumps(receipt, indent=2), flush=True)
result = pytest.main(['-o', 'addopts=', str(repo/'tests/test_optional_awareness_projection.py')+'::test_saved_wire_awareness_preserves_passive_authority_and_rejects_incomplete_proof', '--basetemp='+str(repo/'.artifacts/optional-awareness-identity/installed-case'), '-q', '-s'])
loaded = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items()) if name.startswith('agent_comms') and getattr(module, '__file__', None)}
assert all(Path(path).is_relative_to(candidate) for path in loaded.values()), loaded
receipt.update(pytest_exit=int(result), loaded_core_modules=loaded)
(evidence/'installed-imports.json').write_text(json.dumps(receipt, indent=2)+'\n')
raise SystemExit(result)
