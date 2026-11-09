"""Retain the supplied import map, rejecting a changing input tree."""

import hashlib
import json
import runpy
import sys
from pathlib import Path

ROOT = Path('/home/ts/wt/comms-friction--P17/src')
SCRIPT = Path('/home/ts/wt/import_cycles.py')
OUTPUT = Path(__file__).parent / 'import-graph-current.json'


def fingerprint() -> dict[str, str]:
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / 'agent_comms').rglob('*.py'))
            if '__pycache__' not in path.parts}


before = fingerprint()
sys.argv = [str(SCRIPT), str(ROOT)]
result = runpy.run_path(str(SCRIPT), run_name='__main__')
after = fingerprint()
assert before == after, 'Input tree changed during map capture; result is not a fixed-source comparison'
components = sorted((sorted(component) for component in result['comps']), key=len, reverse=True)
OUTPUT.write_text(json.dumps({
    'source_fingerprints': before,
    'components': components,
    'edges': result['edges'],
    'scope': 'The supplied script: module imports only; no premature-name-read or runtime proof',
}, indent=2) + '\n')
print('Fixed-source component sizes:', [len(component) for component in components])
print('Full member/edge map:', OUTPUT)
print('Raw module SCCs are diagnostic leads, not runtime startup verdicts.')
