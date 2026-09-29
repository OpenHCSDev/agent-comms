import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

repo = Path('/home/ts/wt/comms-selected-cold-compaction-native-20260928')
evidence = repo / 'evidence/native-launch-options'
candidate = repo / '.artifacts/native-launch-options/installed'
commit = '57ec47763340d0214fad5b57a0f210a7d061d95c'
files = subprocess.check_output(['git', 'diff', '--name-only', '942f9824', commit, '--', 'src/agent_comms'], cwd=repo, text=True).splitlines()
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
os.environ['PI_COMPACTION_TEST_PACKAGE'] = '/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent'
os.environ.pop('PYTHONPATH', None)
receipt = {'commit': commit, 'python': sys.executable, 'direct_url': direct, 'changed_helpers': changed, 'native': os.environ['PI_COMPACTION_TEST_PACKAGE'], 'boundary': 'noneditable wheel with actual saved-session/native/ACP loopback; no painted live UI claim'}
(evidence/'installed-imports.json').write_text(json.dumps(receipt, indent=2)+'\n')
print(json.dumps(receipt, indent=2), flush=True)
result = pytest.main(['-o', 'addopts=', str(repo/'tests/test_native_arguments.py')+'::test_saved_native_selection_survives_acp_load_and_one_new_prompt', '--basetemp='+str(repo/'.artifacts/native-launch-options/installed-case'), '-q'])
loaded = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items()) if name.startswith('agent_comms') and getattr(module, '__file__', None)}
assert all(Path(path).is_relative_to(candidate) for path in loaded.values()), loaded
receipt.update(pytest_exit=int(result), loaded_core_modules=loaded)
(evidence/'installed-imports.json').write_text(json.dumps(receipt, indent=2)+'\n')
raise SystemExit(result)
