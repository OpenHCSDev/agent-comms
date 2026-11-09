"""Simulate one retained native batch on immutable Core and owned-Toad sources."""

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot
from nominal_refactor_advisor.codemod_preflight import CodemodOperationPreflightError
from nominal_refactor_advisor.json_reports import json_report_object

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--plan', type=Path, required=True)
parser.add_argument('--core-commit', required=True)
parser.add_argument('--toad-commit', default='HEAD')
args = parser.parse_args()
core = Path('/home/ts/wt/comms-friction--P17')
toad = Path('/home/ts/wt/comms-friction--P23--toad')
sources = {}
revisions = {}
for root, requested, paths in ((core, args.core_commit, ('src', 'tests', 'tools')),
                               (toad, args.toad_commit, ('src', 'tests'))):
    commit = subprocess.run(['git', '-C', str(root), 'rev-parse', requested],
                             check=True, capture_output=True, text=True).stdout.strip()
    revisions[str(root)] = commit
    archive = subprocess.run(['git', '-C', str(root), 'archive', commit, *paths],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as members:
        for member in members:
            if member.isfile() and member.name.endswith('.py'):
                sources[str(root / member.name)] = members.extractfile(member).read().decode()
print(json.dumps({'revisions': revisions, 'source_modules': len(sources)}), flush=True)
plan = CodemodPlanRoot.from_json_value(json.loads(args.plan.read_text())).as_sequence()
print('Native source snapshot building', flush=True)
snapshot = CodemodSourceSnapshot.from_source_mapping(sources)
print('Native ordered simulation starting', flush=True)
cache = Path('/home/ts/.cache/agent-scratch/comms-toad-selector/p23-native-batches')
cache.mkdir(parents=True, exist_ok=True)
proof = cache / f'{args.plan.stem}.simulation.json'
try:
    result = plan.simulate(snapshot)
except CodemodOperationPreflightError as error:
    proof.write_text(json.dumps(json_report_object(error.report), indent=2))
    print(f'Native refusal retained at {proof}', flush=True)
    raise SystemExit(1)
payload = result.execution_payload()
proof.write_text(json.dumps(payload, indent=2))
receipt = {
    'revisions': revisions, 'source_modules': len(sources),
    'is_clean': result.is_clean, 'stage_count': payload['stage_count'],
    'changed_file_paths': payload['simulation']['changed_file_paths'],
    'proof': str(proof), 'proof_sha256': hashlib.sha256(proof.read_bytes()).hexdigest(),
    'plan_sha256': hashlib.sha256(args.plan.read_bytes()).hexdigest(),
    'scope': 'Native simulation only; no product writes',
}
args.plan.with_suffix('.receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2), flush=True)
raise SystemExit(0 if result.is_clean else 1)
