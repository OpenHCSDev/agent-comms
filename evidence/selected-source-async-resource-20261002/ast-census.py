"""Source evidence query using existing NRA parsers, not a dispatch proof or test."""

import ast
from collections import Counter
import hashlib
import gzip
import importlib.util
import json
from pathlib import Path
import sys

ARCHIVE = Path('/home/ts/code/projects/nominal-refactor-advisor/skills/refactor-audit.skill')
sys.path.insert(0, str(ARCHIVE) + '/refactor-audit/scripts')
from audit.findings import Package, _absolute
from audit.repository import Repository

PROJECTION = Path('/home/ts/code/projects/nominal-refactor-advisor/nominal_refactor_advisor/ast_projection.py')
spec = importlib.util.spec_from_file_location('nra_census_projection', PROJECTION)
projection = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = projection
spec.loader.exec_module(projection)
Expression = projection.AstExpressionProjection

CORE = Path('/home/ts/wt/comms-channel-reply-batch-policy-20261002')
BEFORE = '9d557bf469ff1c61494b323a8cccb9fa9dd947c8'
AFTER = 'd45f6287456411254165cbfe6e4d4135ce7ba269'
OWNERS = frozenset((
    'SelectedParticipant', 'SelectedExecution', 'SelectedSource', 'SelectedSourceBatch',
    'Coordination', 'CoordinationSession', 'CoordinationStore', 'CertifiedSourceRead',
    'WireLog', 'ParticipantOwner', 'RegistryOwner', 'WakeAssignment', 'CommittedDelivery',
    'ClaimBatchReceipts', 'PrefixWitness', 'OriginalPrefixWitness', 'AddressedPage',
    'DeliverySources', 'PrivateSendAdmission', 'NativeSourceCursor', 'CursorOwner',
    'CursorPublication', 'InputDrain',
    'SourceCoverage', 'NativeRuntimeInput', 'CurrentNativeCursor', 'PromptBinding',
    'NativeSendStage', 'TriageNativeSend', 'FullNativeSend', 'MessageBus',
    'AcceptedCohort', 'ClaimBatchMembers', 'CohortDeliveryReceipts',
))
OPERATIONS = frozenset((
    '_accept_visible_deliveries', '_receipt_matches', 'accept_delivery_cohort',
    'pending_sealed_assignments', 'sealed_cohort_sequences', 'join_retirement',
    '_expected_assignments', '_immutable_assignment_matches',
))
MEMBERS = frozenset((
    'select', 'sources', 'run_async', '_run_owned', 'lease', 'require_current',
    'require_selected_source', 'require_committed_source', 'require_registry',
    'addressed_sources', 'addressed_deliveries', 'certified_read', 'references',
    'capture_deliveries', 'finish_turn', 'lease_live_turn_with_admission',
))
snapshots = (
    ('core-before', CORE, BEFORE), ('core-after', CORE, AFTER),
    ('toad-consumer', Path('/home/ts/wt/toad-receiving-native5-batch490-20261001'),
     '00cbf62c8d69857df03cfed374f6a6d0f0e4240b'),
    ('textual-dependency', Path('/home/ts/wt/textual-main'),
     '23822923a02b75a1fad10751d97dc009c36b598b'),
)
output = {
    'kind': 'AST_SOURCE_EVIDENCE_NOT_DYNAMIC_RESOLUTION',
    'tools': {'archive': str(ARCHIVE), 'archive_sha256': hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
              'parser': 'audit.findings.Package.load', 'repository': 'audit.repository.Repository',
              'projection': str(PROJECTION), 'projection_sha256': hashlib.sha256(PROJECTION.read_bytes()).hexdigest()},
    'reference_gate': {'repository': '/home/ts/code/projects/openhcs',
                       'commit': '5e8812ee83d0dc8714392445bad3e32fc47a1755',
                       'path': 'tests/unit/test_cellprofiler_static_deletion_gates.py'},
    'limits': [
        'Imported/dotted spellings and class ancestry are source evidence, not native MRO or descriptor execution.',
        'Receiver variables, monkeypatches, shadowing and dynamic getattr/import dispatch remain unproved; candidate references are retained.',
        'Historical installed-original producer scripts are parsed and reported, not migrated to the current producer API.',
        'Non-Python native/provider protocol implementations and generated/embedded source are not parsed by this Python query; matching string literals are retained as candidates.',
        'External stdlib/SQLite and official ACP SDK runtime resolution is not proved by scanning declared project/dependency roots.',
    ],
    'snapshots': [],
}
for label, location, revision in snapshots:
    repo = Repository(location)
    tracked = tuple(p for p in repo.git('ls-tree', '-r', '--name-only', revision).splitlines() if p.endswith('.py'))
    roots = sorted({p.split('/')[0] for p in tracked if '/' in p})
    if any(p.startswith('src/agent_comms/') for p in tracked):
        roots.remove('src')
        roots.append('src/agent_comms')
    packages = [Package.load(repo, revision, root) for root in roots]
    modules = [module for package in packages for module in package.modules]
    failures = [path for package in packages for path in package.unparsed]
    omitted = sorted(set(tracked) - {m.path for m in modules} - set(failures))
    report = {'label': label, 'repository': str(location), 'revision': revision,
              'declared_roots': roots, 'tracked_python_files': len(tracked),
              'parsed_files': len(modules), 'parse_failures': failures, 'enumeration_omissions': omitted,
              'module_sha256': {m.path: hashlib.sha256(m.text.encode()).hexdigest() for m in modules},
              'declarations': [], 'imports': [], 'references': [], 'owned_statements': [], 'classes': []}
    for module in modules:
        parents = {id(child): parent for parent in ast.walk(module.tree) for child in ast.iter_child_nodes(parent)}
        alias_candidates = set()
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                alias_candidates.update(a.asname or a.name for a in node.names if a.name in OWNERS | OPERATIONS | MEMBERS)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                # Conservative alias candidates, including tuple assignment.
                # No lexical shadowing or runtime identity is inferred here.
                if any(Expression.terminal_name(n) in OWNERS | OPERATIONS | MEMBERS for n in ast.walk(node.value)):
                    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                    alias_candidates.update(n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name))
        for node in ast.walk(module.tree):
            lineage, parent, resource_context = [], parents.get(id(node)), ''
            while parent is not None:
                if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    lineage.append(parent)
                if not resource_context and isinstance(parent, (ast.With, ast.AsyncWith)):
                    resource_context = type(parent).__name__
                parent = parents.get(id(parent))
            lineage.reverse()
            context = '.'.join(n.name for n in lineage)
            owner = next((n.name for n in reversed(lineage) if isinstance(n, ast.ClassDef)), '')
            owned = owner in OWNERS or any(n.name in OPERATIONS for n in lineage)
            site = {'path': module.path, 'line': getattr(node, 'lineno', 0), 'context': context}
            if isinstance(node, ast.ClassDef):
                record = {**site, 'name': node.name, 'bases': [ast.unparse(b) for b in node.bases]}
                report['classes'].append(record)
                if node.name in OWNERS:
                    report['declarations'].append({**record, 'kind': 'class', 'decorators': [ast.unparse(d) for d in node.decorator_list],
                        'annotated_fields': {ast.unparse(n.target): ast.unparse(n.annotation) for n in node.body if isinstance(n, ast.AnnAssign)},
                        'direct_members': [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (owned or node.name in OPERATIONS):
                report['declarations'].append({**site, 'kind': type(node).__name__, 'name': node.name,
                    'arguments': ast.unparse(node.args), 'returns': ast.unparse(node.returns) if node.returns else '',
                    'decorators': [ast.unparse(d) for d in node.decorator_list]})
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                base = _absolute(module.package, node.module, node.level) if isinstance(node, ast.ImportFrom) else ''
                report['imports'].append({**site, 'source': ast.unparse(node), 'absolute_from': base,
                    'names': [{'original': a.name, 'bound': a.asname or a.name} for a in node.names]})
            if isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
                expression = node.func if isinstance(node, ast.Call) else node
                terminal = Expression.terminal_name(expression)
                if terminal in OWNERS | OPERATIONS | MEMBERS | alias_candidates:
                    report['references'].append({**site, 'kind': type(node).__name__, 'terminal': terminal,
                        'spelling': Expression.qualified_name(expression) or ast.unparse(expression),
                        'awaited': isinstance(parents.get(id(node)), ast.Await),
                        'resource_context': resource_context,
                        'resolution': 'nominal spelling candidate; receiver/import binding and dynamic dispatch require semantic review'})
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and any(n in node.value for n in OWNERS | OPERATIONS):
                report['references'].append({**site, 'kind': 'string/embedded-source candidate', 'source': node.value[:1000],
                    'resolution': 'not interpreted/executed by this query'})
            if owned and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.If, ast.IfExp, ast.Compare, ast.Assert, ast.Return, ast.With, ast.AsyncWith, ast.Await, ast.Raise, ast.Call, ast.Attribute)):
                report['owned_statements'].append({**site, 'kind': type(node).__name__, 'source': ast.unparse(node)[:1800]})
    counts = Counter(d['name'] for d in report['declarations'] if d['kind'] == 'class' and d['path'].startswith('src/'))
    report['production_owner_definition_counts'] = dict(sorted(counts.items()))
    report['duplicate_production_nominal_definitions'] = {n: c for n, c in counts.items() if c > 1}
    report['qualified_production_definitions'] = {
        f"{d['path']}:{d['context'] + '.' if d['context'] else ''}{d['name']}": d['line']
        for d in report['declarations'] if d['kind'] == 'class' and d['path'].startswith('src/')
    }
    report['inherited_source_edges'] = [c for c in report['classes'] if c['name'] in OWNERS or any(b.rsplit('.', 1)[-1] in OWNERS for b in c['bases'])]
    output['snapshots'].append(report)
    print(label, 'parsed', len(modules), 'failures', len(failures), 'omitted', len(omitted), 'definitions', dict(counts), flush=True)
destination = Path(__file__).with_name('ast-before-after.json.gz')
destination.write_bytes(gzip.compress((json.dumps(output, indent=2) + '\n').encode(), mtime=0))
print('output', destination, 'bytes', destination.stat().st_size)
if any(s['parse_failures'] or s['enumeration_omissions'] for s in output['snapshots']):
    raise SystemExit('Source census incomplete; omissions retained, not reported as zero.')
