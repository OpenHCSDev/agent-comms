"""Use the existing child-goal capability at the Registration boundary."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import subprocess

from nominal_refactor_advisor.codemod import (
    CodemodPlanDocument, CodemodPlanSequence, CodemodSourceSnapshot,
    PatchTargetOperation, RefactorRecipe, SourceRewriteTarget, SourceTextReplacement,
)
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.source_insertion import EnsureImportOperation, RemoveImportBindingsOperation

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent / 'p23-selected-owner-7b/projected'
LIVE = Path('/home/ts/wt/comms-friction--P17')
HEAD = '27e59c079710b9d5405725edb82d3f6fe1a9a047'
MODULE = 'src/agent_comms/registry/registration.py'


def target(name=None):
    return SourceRewriteTarget(file_path=str(ROOT / MODULE), qualname=name)


def document(name, operations):
    return CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=name, operations=tuple(operations)),))


def blob(content):
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def main():
    entries = subprocess.run(
        ['git', '-C', str(LIVE), 'ls-tree', '-r', '--full-tree', HEAD, 'src', 'tests', 'tools', 'pyproject.toml'],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.splitlines()
    tree = {path: metadata.split()[2] for metadata, path in (line.split('\t', 1) for line in entries)}
    sources = {}
    for path in ROOT.rglob('*.py'):
        relative = str(path.relative_to(ROOT))
        content = path.read_bytes()
        assert tree.get(relative) == blob(content), relative
        sources[str(path)] = content.decode()
    assert set(path for path in tree if path.endswith('.py')) == set(str(Path(path).relative_to(ROOT)) for path in sources)
    assert tree['pyproject.toml'] == blob((ROOT / 'pyproject.toml').read_bytes())
    (OUT / 'source-input.json').write_text(json.dumps({
        'head': HEAD, 'input_projection': str(ROOT),
        'matched_python_files': len(sources), 'configuration_blob': tree['pyproject.toml'],
        'reconstruction': '../p23-selected-owner-7b/before-source.tar.gz plus its already-applied selected-owner.plan.json',
    }, indent=2) + '\n')
    snapshot = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT / 'src',))
    stages = (
        document('import-existing-child-goal-capability', (
            EnsureImportOperation(target=target(), import_source='from agent_comms.planning.goal_mutation import GoalFork'),
        )),
        document('use-existing-goal-fork-contract-in-child-registration', tuple(
            PatchTargetOperation(target=target('Registration.' + name), replacements=(
                SourceTextReplacement(old_source='goals: goal_management.Goals',
                                      new_source='goals: GoalFork[Thread, goals.GoalDeclaration]'),
            ))
            for name in ('register_child', 'claim_slot', '_child_in')
        )),
        document('delete-unread-concrete-goal-manager-import', (
            RemoveImportBindingsOperation(target=target(), bound_names=('goal_management',)),
        )),
    )
    for index, stage in enumerate(stages):
        preflight = stage.preflight(snapshot)
        (OUT / f'stage-{index}.preflight.json').write_text(json.dumps(json_report_object(preflight.report), indent=2) + '\n')
        result = preflight.simulate()
        assert result.is_clean
        snapshot = result.required_after_snapshot
        print(f'{index + 1} native-clean stages', flush=True)
    before = ast.parse(sources[str(ROOT / MODULE)])
    after_source = snapshot.sources_by_file_path[str(ROOT / MODULE)]
    after = ast.parse(after_source)
    before_registration = next(node for node in before.body if isinstance(node, ast.ClassDef) and node.name == 'Registration')
    after_registration = next(node for node in after.body if isinstance(node, ast.ClassDef) and node.name == 'Registration')
    for original, changed in zip(before_registration.body, after_registration.body, strict=True):
        match original, changed:
            case ast.FunctionDef(), ast.FunctionDef():
                assert original.name == changed.name
                assert tuple(map(ast.dump, original.body)) == tuple(map(ast.dump, changed.body))
                assert tuple(map(ast.dump, original.args.defaults)) == tuple(map(ast.dump, changed.args.defaults))
                assert tuple(None if item is None else ast.dump(item) for item in original.args.kw_defaults) == tuple(None if item is None else ast.dump(item) for item in changed.args.kw_defaults)
            case _:
                assert ast.dump(original) == ast.dump(changed)
    plan = CodemodPlanSequence(documents=stages)
    encoded = json.dumps(json_report_object(plan), indent=2) + '\n'
    (OUT / 'goal-fork.plan.json').write_text(encoded)
    (OUT / 'goal-fork-handoff.plan.json').write_text(encoded.replace(str(ROOT), str(LIVE)))
    (OUT / 'registration.after.py.txt').write_text(after_source)
    print('Original Registration procedures, defaults and field declarations match; source simulation only', flush=True)


if __name__ == '__main__':
    main()
