"""Simulate the ingress batch on immutable Core and owned-Toad source through NRA."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot
from nominal_refactor_advisor.codemod_preflight import CodemodOperationPreflightError
from nominal_refactor_advisor.json_reports import json_report_object

output = Path(__file__).parent
core = Path('/home/ts/wt/comms-friction--P17')
toad = Path('/home/ts/wt/comms-friction--P23--toad')
core_commit = '622c26d425e2482d3b430e4c747ebcdeb83a5e31'
toad_commit = subprocess.run(['git', '-C', str(toad), 'rev-parse', 'HEAD'],
                             check=True, capture_output=True, text=True).stdout.strip()
sources = {}
for root, commit, paths in ((core, core_commit, ('src', 'tests', 'tools')),
                            (toad, toad_commit, ('src', 'tests'))):
    archive = subprocess.run(['git', '-C', str(root), 'archive', commit, *paths],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as members:
        for member in members:
            if member.isfile() and member.name.endswith('.py'):
                sources[str(root / member.name)] = members.extractfile(member).read().decode()
print(json.dumps({'core': core_commit, 'toad': toad_commit,
                  'source_modules': len(sources)}), flush=True)
plan = CodemodPlanRoot.from_json_value(json.loads((output / 'low-contracts.plan.json').read_text())).as_sequence()
print('Native source snapshot building', flush=True)
snapshot = CodemodSourceSnapshot.from_source_mapping(sources)
print('Native ordered simulation starting', flush=True)
try:
    result = plan.simulate(snapshot)
except CodemodOperationPreflightError as error:
    (output / 'low-contracts.simulation.json').write_text(json.dumps(json_report_object(error.report), indent=2))
    print('Native refusal retained in low-contracts.simulation.json', flush=True)
    raise SystemExit(1)
(output / 'low-contracts.simulation.json').write_text(json.dumps(result.execution_payload(), indent=2))
print(json.dumps({'is_clean': result.is_clean, 'scope': 'native simulation only; no product writes'}), flush=True)
raise SystemExit(0 if result.is_clean else 1)
