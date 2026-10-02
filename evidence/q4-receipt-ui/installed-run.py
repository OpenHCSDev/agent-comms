"""Verify the noneditable changed owners, then run the affected native journey."""
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

repo = Path(__file__).resolve().parents[2]
installed = repo / '.artifacts/q4-installed/runtime'
evidence = Path(__file__).resolve().parent
paths = subprocess.check_output(
    ['git', 'diff', '--name-only', '11dcae27', 'HEAD', '--', 'src/agent_comms'],
    cwd=repo, text=True,
).splitlines()
modules = {}
for path in paths:
    name = path.removeprefix('src/').removesuffix('.py').replace('/', '.')
    actual = Path(importlib.import_module(name).__file__).resolve()
    assert actual.is_relative_to(installed), actual
    assert actual.read_bytes() == (repo / path).read_bytes(), path
    modules[name] = str(actual)
direct = json.loads(importlib.metadata.distribution('agent-comms').read_text('direct_url.json'))
assert not direct.get('dir_info', {}).get('editable', False)
assert not os.environ.get('PYTHONPATH')
result = pytest.main([
    '-o', 'addopts=', '-q',
    str(repo / 'tests/test_extension_ui_native.py'),
    '--basetemp=' + str(repo / '.artifacts/q4-installed/runtime-case'),
])
loaded = {
    name: str(Path(module.__file__).resolve())
    for name, module in tuple(sys.modules.items())
    if name.startswith('agent_comms') and getattr(module, '__file__', None)
}
assert all(Path(path).is_relative_to(installed) for path in loaded.values()), loaded
(evidence / 'installed-imports.json').write_text(json.dumps({
    'python': sys.executable, 'direct_url': direct, 'changed_owners': modules,
    'loaded_core_modules': loaded, 'pytest_exit': int(result),
    'boundary': 'noneditable wheel, pinned native, loopback provider, real saved history, ACP socket, receipt and dialog replies',
}, indent=2) + '\n')
raise SystemExit(result)
