import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

repo = Path('/home/ts/wt/comms-selected-cold-compaction-native-20260928')
evidence = repo / 'evidence/selected-dispatch-ownership'
candidate = repo / '.artifacts/selected-dispatch-ownership/installed'
commit = '02c0e120b445780ef7c771a6d0a1565250428480'
files = subprocess.check_output(['git', 'diff', '--name-only', 'baec4fe1^', commit, '--', 'src/agent_comms'], cwd=repo, text=True).splitlines()
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
receipt = {'commit': commit, 'python': sys.executable, 'direct_url': direct, 'changed_helpers': changed, 'boundary': 'noneditable wheel; actual pinned native triage and full dispatch, saved-session reuse, tools, reply; channel return actual owner/ACP observation; only local provider responses'}
(evidence/'installed-imports.json').write_text(json.dumps(receipt, indent=2)+'\n')
print(json.dumps(receipt, indent=2), flush=True)
os.environ['AC_NATIVE_COPIED_PACKAGE'] = '/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent'
os.environ['PI_COMPACTION_TEST_PACKAGE'] = os.environ['AC_NATIVE_COPIED_PACKAGE']
result = pytest.main(['-o', 'addopts=', str(repo/'tests/test_selected_execution_native.py')+'::test_native_full_four_tools_publish_and_release[False-False-True]', str(repo/'tests/test_native_channel_reply_roundtrip.py'), '--basetemp='+str(repo/'.artifacts/selected-dispatch-ownership/installed-case'), '-q', '-s'])
loaded = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items()) if name.startswith('agent_comms') and getattr(module, '__file__', None)}
assert all(Path(path).is_relative_to(candidate) for path in loaded.values()), loaded
receipt.update(pytest_exit=int(result), loaded_core_modules=loaded)
(evidence/'installed-imports.json').write_text(json.dumps(receipt, indent=2)+'\n')
raise SystemExit(result)
