"""Project the original P23 scripts through P115 without editing the shared pair."""

import hashlib
import json
import subprocess
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot

ARTIFACTS = Path(__file__).resolve().parent
TOAD = Path('/home/ts/wt/comms-friction--P23--toad')
HEAD = 'c0020780a3f8a67550575d193eb8fa9222241b9d'
NRA = Path('/home/ts/wt/comms-toad-selector--P115--nominalrefactoradvisor')
SCRIPTS = ('command_target_probe', 'goal_read_probe')
sources = {}
for relative in (
    *(f'tests/native/{name}.py' for name in SCRIPTS),
    'tests/support/__init__.py',
):
    sources[str(TOAD / relative)] = subprocess.run(
        ['git', '-C', str(TOAD), 'show', f'{HEAD}:{relative}'],
        check=True, capture_output=True, text=True,
    ).stdout
plan = CodemodPlanRoot.from_json_value(
    json.loads((ARTIFACTS / 'whole-script-placement.plan.json').read_text())
).as_sequence()
result = plan.simulate(CodemodSourceSnapshot.from_source_mapping(sources))
assert result.is_clean, result
preserved = []
for name in SCRIPTS:
    old = str(TOAD / f'tests/native/{name}.py')
    new = str(TOAD / f'tests/support/{name}.py')
    assert result.simulation.rewritten_sources[new] == sources[old]
    assert old in result.simulation.deleted_file_paths
    preserved.append({
        'old': old,
        'new': new,
        'source_sha256': hashlib.sha256(sources[old].encode()).hexdigest(),
        'body_preserved': True,
    })
report = {
    'toad_head': HEAD,
    'nra_head': subprocess.run(
        ['git', '-C', str(NRA), 'rev-parse', 'HEAD'],
        check=True, capture_output=True, text=True,
    ).stdout.strip(),
    'nra_has_uncommitted_fix': bool(subprocess.run(
        ['git', '-C', str(NRA), 'status', '--porcelain', '--untracked-files=no'],
        check=True, capture_output=True, text=True,
    ).stdout),
    'native_simulation_clean': result.is_clean,
    'preserved': preserved,
    'changed_paths': result.simulation.changed_file_paths,
    'scope': 'Native simulation of original scripts and support package only; no application, original-probe execution, tests or shared edits. Existing tracked caller absence was established separately; this is not a new full-tree scan.',
}
(ARTIFACTS / 'p115-original-script-simulation.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2), flush=True)
