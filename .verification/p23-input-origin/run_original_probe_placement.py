"""Move private copies natively and compare the original probes on the actual P23 classes."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from nominal_refactor_advisor.codemod import (
    CodemodSourceSnapshot,
    RefactorRecipe,
    RelocateModuleToNewModuleOperation,
    SourceRewriteTarget,
)

ARTIFACTS = Path(__file__).resolve().parent
TOAD = Path('/home/ts/wt/comms-friction--P23--toad')
CORE = Path('/home/ts/wt/comms-friction--P17')
NRA = Path('/home/ts/wt/comms-toad-selector--P115--nominalrefactoradvisor')
PYTHON = Path('/home/ts/.cache/toad/test-env/4f4f4f5751050005/bin/python')
ROOT = ARTIFACTS / 'p115-original-probe-consumer'
HEAD = 'c0020780a3f8a67550575d193eb8fa9222241b9d'
CASES = (('command_target_probe', 'toad.screens.main'), ('goal_read_probe', 'toad.goal_interaction'))


def head(repository: Path) -> str:
    return subprocess.run(['git', '-C', str(repository), 'rev-parse', 'HEAD'],
                          check=True, capture_output=True, text=True).stdout.strip()


def run_probe(path: Path, first_module: str) -> dict[str, object]:
    result = subprocess.run([str(PYTHON), str(path), first_module], cwd=ROOT,
                            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    return {'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


assert not ROOT.exists(), 'Retain an existing consumer result; do not replay it'
ROOT.mkdir(parents=True)
revisions = {'core': head(CORE), 'toad': head(TOAD), 'nra': head(NRA)}
sources = {}
before = {}
recipe = RefactorRecipe('move-original-private-probes')
for name, first_module in CASES:
    relative = f'tests/native/{name}.py'
    source = subprocess.run(['git', '-C', str(TOAD), 'show', f'{HEAD}:{relative}'],
                            check=True, capture_output=True, text=True).stdout
    old = ROOT / relative
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(source)
    sources[name] = source
    recipe = recipe.with_operation(RelocateModuleToNewModuleOperation(
        target=SourceRewriteTarget(file_path=str(old)),
        destination_path=str(ROOT / f'tests/support/{name}.py'),
    ))
support = ROOT / 'tests/support/__init__.py'
support.parent.mkdir(parents=True, exist_ok=True)
support.write_text('')
for name, first_module in CASES:
    before[name] = run_probe(ROOT / f'tests/native/{name}.py', first_module)
result = recipe.simulate(CodemodSourceSnapshot.from_modules(parse_python_modules(ROOT)))
assert result.is_clean, result
result.apply()
records = []
for name, first_module in CASES:
    destination = ROOT / f'tests/support/{name}.py'
    assert destination.read_text() == sources[name]
    after = run_probe(destination, first_module)
    records.append({
        'script': name,
        'first_module': first_module,
        'source_sha256': hashlib.sha256(sources[name].encode()).hexdigest(),
        'body_preserved': True,
        'before': before[name],
        'after': after,
        'same_exit_and_stdout': before[name]['exit'] == after['exit'] and before[name]['stdout'] == after['stdout'],
    })
report = {
    'revisions_before': revisions,
    'revisions_after': {'core': head(CORE), 'toad': head(TOAD), 'nra': head(NRA)},
    'interpreter': str(PYTHON),
    'workspace': str(ROOT),
    'native_application': 'private source copies only',
    'records': records,
    'scope': 'Actual production classes from the existing P23 environment; two original scripts before/after one native private relocation. No shared-pair write, fake provider, suite or paid call.',
}
output = ARTIFACTS / 'p115-original-probe-consumer.json'
output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2), flush=True)
assert report['revisions_before'] == report['revisions_after']
assert all(row['same_exit_and_stdout'] and row['before']['exit'] == 0 for row in records)
