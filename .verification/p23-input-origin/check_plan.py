"""Check proposal geometry and unchanged permission bodies, without applying it."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parent
plan = json.loads((ROOT / 'input-origin-v3.plan.json').read_text())
patches = [operation for stage in plan['stages'] for recipe in stage['recipes']
           for operation in recipe['operations']
           if operation['operation'] == 'patch_target']
projected = {}
checks = 0
for operation in patches:
    path = Path(operation['file_path'])
    source = projected.get(path, path.read_text())
    for change in operation['replacements']:
        count = source.count(change['old_source'])
        assert count == 1, (str(path), change['old_source'], count)
        source = source.replace(change['old_source'], change['new_source'], 1)
        checks += 1
    ast.parse(source)
    projected[path] = source

origin = Path('/home/ts/wt/comms-friction--P17/src/agent_comms/input_origin.py')
before = ast.parse(origin.read_text())
after = ast.parse(projected[origin])


def methods(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    family = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == 'HumanInputOrigin')
    return {node.name: node for node in family.body if isinstance(node, ast.FunctionDef)}


original = methods(before)
proposed = methods(after)
for name in ('capture', '_require_owner', 'require_ingress', 'matches_owner', 'applies'):
    assert [ast.dump(node) for node in original[name].body] == [
        ast.dump(node) for node in proposed[name].body], name
    print(f'{name}: original body unchanged')

assert sum(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr == 'snapshot_with_authority'
           for node in ast.walk(proposed['capture'])) == 2
task_source = origin.with_name('task_sources.py')
before_tasks = ast.parse(task_source.read_text())
after_tasks = ast.parse(projected[task_source])


def publication_method(tree: ast.Module) -> ast.FunctionDef:
    pin = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == 'NativeInputConstraintPin')
    return next(node for node in pin.body
                if isinstance(node, ast.FunctionDef) and node.name == 'require_wording_publication')


old_method = publication_method(before_tasks)
new_method = publication_method(after_tasks)
assert [ast.dump(node) for node in old_method.body[4:]] == [
    ast.dump(node) for node in new_method.body[1:]]
print('NativeInputConstraintPin: origin/namespace/author/recipient/project checks unchanged after original lookup')
for stage in plan['stages']:
    for recipe in stage['recipes']:
        for operation in recipe['operations']:
            if operation['operation'] == 'create_file':
                ast.parse(operation['source'])
print(f'{checks} unique patch hunks; {len(projected)} modules parse; two authority acquisitions retained')
base_operations = [operation for stage in plan['stages'] for recipe in stage['recipes']
                   for operation in recipe['operations']
                   if operation['operation'] in ('add_class_base', 'replace_class_base')]
for operation in base_operations:
    path = Path(operation['file_path'])
    classes = [node for node in ast.parse(path.read_text()).body
               if isinstance(node, ast.ClassDef)
               and node.name == operation['target_qualname']]
    assert len(classes) == 1, operation
print(f'{len(base_operations)} native base operations each identify one original provider declaration')
api = ast.parse((ROOT / 'input_publication_read.py.txt').read_text())
assert all(node.module in ('abc', 'agent_comms.input_ingress')
           for node in api.body if isinstance(node, ast.ImportFrom))
assert not any(isinstance(node, ast.Import) for node in api.body)
assert any(isinstance(node, ast.ClassDef) and node.name == 'InputPublicationRead'
           for node in api.body)
print('Low publication API imports only abc and input_ingress, never its implementation or stores')
print('Proposal-only checks; native NRA simulation, import closure and consumer tests NOT run')
