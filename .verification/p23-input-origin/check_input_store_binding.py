"""Check this one binding proposal against current source; never apply it."""

import ast
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
ROOT = Path('/home/ts/wt/comms-friction--P17/src/agent_comms')
original = (ROOT / 'input_disposition.py').read_text()
plan = json.loads((HERE / 'input-store-bus-binding.plan.json').read_text())
projected = original
for stage in plan['stages']:
    for recipe in stage['recipes']:
        for operation in recipe['operations']:
            if operation['operation'] == 'patch_target':
                for change in operation['replacements']:
                    assert projected.count(change['old_source']) == 1, change
                    projected = projected.replace(change['old_source'], change['new_source'], 1)
old_import = 'from agent_comms.bus import Bus, BusReader'
assert projected.count(old_import) == 1
projected = projected.replace(old_import, 'import agent_comms.bus as bus', 1)
before = ast.parse(original)
after = ast.parse(projected)
assert not any(isinstance(node, ast.Name) and node.id in ('Bus', 'BusReader')
               for node in ast.walk(after))
assert any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
           and any(item.name == 'annotations' for item in node.names) for node in after.body)


class OriginalBusNames(ast.NodeTransformer):
    def visit_Attribute(self, node):
        match node:
            case ast.Attribute(value=ast.Name(id='bus'), attr=('Bus' | 'BusReader') as name):
                return ast.copy_location(ast.Name(id=name, ctx=ast.Load()), node)
        return self.generic_visit(node)


normalized = OriginalBusNames().visit(after)
for old_class in (node for node in before.body if isinstance(node, ast.ClassDef)):
    new_class, = [node for node in normalized.body
                  if isinstance(node, ast.ClassDef) and node.name == old_class.name]
    assert ast.dump(old_class) == ast.dump(new_class), old_class.name
print('All original class declarations and method bodies match after original-module name normalization')
print('Original BusReader annotation remains postponed; original Bus constructor remains in harness_attempts')
print('No bare Bus/BusReader lookup remains at declaration time; no local or guarded import added')
print('Source SHA256:', hashlib.sha256(original.encode()).hexdigest())
print('Plan SHA256:', hashlib.sha256((HERE / 'input-store-bus-binding.plan.json').read_bytes()).hexdigest())
print('Proposal only; native apply and complete cold/runtime consumers NOT run')
