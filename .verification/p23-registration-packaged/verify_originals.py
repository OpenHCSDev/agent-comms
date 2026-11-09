"""Compare original procedures, concrete defaults and stored declarations as syntax."""
from __future__ import annotations

import ast
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent


def classes(root, module):
    tree = ast.parse((root / 'src/agent_comms' / (module + '.py')).read_text())
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def procedure(node):
    return (
        node.name,
        tuple(ast.dump(item) for item in node.body),
        tuple(ast.dump(item) for item in node.args.defaults),
        tuple(None if item is None else ast.dump(item) for item in node.args.kw_defaults),
        tuple(ast.dump(item) for item in node.decorator_list),
    )


def declaration(node):
    match node:
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            return procedure(node)
        case _:
            return ast.dump(node)


def main():
    checked = []
    for module in (
        'registry/registration', 'registry/agent_activity',
        'messaging/message_notification', 'registry/registry_store',
    ):
        original = classes(OUT / 'before', module)
        projected = classes(OUT / 'projected', module)
        assert original.keys() == projected.keys(), module
        for name, before in original.items():
            after = projected[name]
            assert tuple(map(declaration, before.body)) == tuple(map(declaration, after.body)), (module, name)
            checked.append(f'{module}.{name}')
    original_reads = classes(OUT / 'before', 'registry/activity_registry_reads')
    after_reads = classes(OUT / 'projected', 'registry/activity_registry_reads')
    moved_reads = classes(OUT / 'projected', 'registry/registration_reads')
    assert ast.dump(original_reads['ActivityRegistryRead']) == ast.dump(after_reads['ActivityRegistryRead'])
    assert ast.dump(original_reads['RegistryPathRead']) == ast.dump(moved_reads['RegistryPathRead'])
    result = {
        'scope': 'source procedures, concrete defaults and stored declarations; no runtime or C3 claim',
        'unchanged_classes': checked,
        'unchanged_read_declarations': ['ActivityRegistryRead', 'RegistryPathRead'],
        'abstract_routing_default': 'ellipsis contract marker; concrete Registration and AgentActivity defaults remain NoTurnRouting()',
    }
    (OUT / 'originals.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
