"""Read the installed candidate through the original cohort and audit owners."""
from pathlib import Path
import ast
import hashlib
import json
import subprocess
import sys

from refactor_audit.findings import Package
from refactor_audit.repository import Repository

repository = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
candidate = Path('/home/ts/wt/toad-prompt-action-owner-20261002/.artifacts/context637-tool430-native59-receiving-20261004')
head = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
owners = {'ReviewedArtifact', 'CohortActivation', 'InstalledSourceProof', 'ReviewedRetainedSummaryCohort'}
roots, sites = [], []
for root in ('src/agent_comms', 'tools'):
    package = Package.load(Repository(repository), head, root)
    roots.append({'root': root, 'parsed': len(package.modules), 'unparsed': list(package.unparsed)})
    assert not package.unparsed
    for module in package.modules:
        for node in ast.walk(module.tree):
            owner = isinstance(node, ast.ClassDef) and node.name in owners
            call = isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in ('CohortActivation', 'ReviewedRetainedSummaryCohort')
                or isinstance(node.func, ast.Attribute) and node.func.attr in ('require_original', 'require_activation')
                and 'cohort' in ast.unparse(node.func.value))
            if owner or call:
                sites.append({'path': module.path, 'line': node.lineno, 'kind': 'owner' if owner else 'consumer',
                              'source': ast.get_source_segment(module.text, node)})
source = (repository / 'tools/cutover/publish_retained_summary.py').read_text()
assert "self.activation.path != self.target / 'activation.json'" not in source
assert 'if activation.stage != self.target:' in source
after = {'head': head, 'parser': 'Original refactor_audit.Package', 'roots': roots, 'sites': sites,
         'dynamic_resolution': 'Lexical named cohort constructors and cohort receivers; generic dynamic receivers are not claimed resolved',
         'competing_storage_predicate': 0, 'typed_stage_predicate': 1}
(evidence / 'after.json').write_text(json.dumps(after, indent=2) + '\n')

sys.path.insert(0, str(repository / 'tools/cutover'))
from publish_retained_summary import (ReviewedArtifact, ReviewedRetainedSummaryCohort,
                                     COMMANDS, LINKS, read_active_route, digest)
from agent_comms.field_codec import FieldCodec
from agent_comms.native_package import MANIFEST, COMPACTION_HELPER, OWNER_INSTRUCTIONS

owner = json.loads((candidate / 'ownership.json').read_text())
target = Path(owner['prefix'])
assert sys.executable == str(target / 'bin/python')
protected = json.loads((candidate / 'protected-original431.json').read_text())
def unchanged():
    for name, expected in protected.items():
        assert digest(Path(name)) == expected, name
unchanged()
def artifact(path):
    return ReviewedArtifact(path, digest(path))
gates = (
    Path('/home/ts/wt/comms-field-codec-closure-20260928/evidence/context-segment-loading-20261004/installed-current-preview/terminal-receipt.json'),
    Path('/home/ts/wt/comms-field-codec-closure-20260928/evidence/context-segment-loading-20261004/installed-current-preview/app-receipt.json'),
    Path('/home/ts/wt/toad-viewport-raster-cpu-continuation-20261001/evidence/tool-subtree-publication-lifetime-20261004/installed-app-terminal.json'),
    Path('/home/ts/wt/toad-regression-widget-cost-20261001/evidence/recorded-context-reader-20261004/installed13/terminal-receipt.json'),
    Path('/home/ts/wt/toad-regression-widget-cost-20261001/evidence/recorded-context-reader-20261004/installed14/terminal-receipt.json'),
)
current = (LINKS / COMMANDS[0]).readlink().parent.parent
cohort = ReviewedRetainedSummaryCohort(target, current / 'bin/python', current,
    read_active_route(), Path(owner['native']), artifact(candidate / 'candidate-activation.json'),
    artifact(candidate / 'source-proof.json'), tuple(artifact(path) for path in gates))
cohort.require_original()
forced = []
for path, installed in (('stack/pi-native.sha256', MANIFEST),
                        ('stack/native-compaction-commit-child.mjs', COMPACTION_HELPER),
                        ('.pi/APPEND_SYSTEM.md', OWNER_INSTRUCTIONS)):
    raw = subprocess.check_output(['git', '-C', str(repository), 'show', owner['core'] + ':' + path])
    assert installed.read_bytes() == raw, path
    forced.append({'source': path, 'installed': str(installed), 'sha256': hashlib.sha256(raw).hexdigest()})
unchanged()
proof = json.loads((candidate / 'source-proof.json').read_text())
assert sum(item['files'] for item in proof['sources']) == 942
receipt = {'state': 'PASS', 'source_head': head, 'installed_core': owner['core'],
           'cohort': FieldCodec.encode(cohort), 'installed_git_assets': 942,
           'forced_native_assets': forced, 'protected_original431': len(protected),
           'protected_original431_unchanged': True, 'candidate_activation_outside_prefix': True,
           'package_restaged': False, 'public_effects': False, 'publisher_called': False,
           'audience_or_client_admission_called': False, 'app_sdk_provider_launched': False}
(evidence / 'installed-cohort-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({key: receipt[key] for key in ('state', 'source_head', 'installed_git_assets', 'protected_original431', 'protected_original431_unchanged')}))
