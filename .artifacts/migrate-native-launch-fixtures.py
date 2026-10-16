"""One-use fixture migration, based on the retained NRA syntax inventory.

Only direct tracked calls are edited. **request in test_native_pi.py is the
reviewed provider/model-bearing request. Other families are reported, not guessed.
"""
import ast
from pathlib import Path
from nominal_refactor_advisor.ast_tools import module_syntax_index, parse_python_module_roots

root = Path('/home/ts/wt/core-native-runtime')
paths = {'test_native_pi.py'}
for module in parse_python_module_roots((root/'tests',), use_parse_cache=False, parse_workers=1):
    if module.path.name not in paths:
        continue
    data = module.path.read_bytes()
    starts = [0]
    for line in data.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    changes = []
    for _, node in module_syntax_index(module.module).indexed_nodes_of_type(ast.Call):
        if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            continue
        if (node.func.value.id, node.func.attr) not in {
            ('NativePiRpcLaunch', 'tracked'), ('TrackedTurnSession', 'execute')
        }:
            continue
        keywords = {kw.arg for kw in node.keywords}
        supplied = []
        if 'runtime' not in keywords:
            supplied.append('runtime=LaunchRuntime.running()')
        if 'arguments' not in keywords:
            supplied.append('arguments=NativeThreadDefaults.offline().arguments')
        if None not in keywords:
            for key in ('provider', 'model'):
                if key not in keywords:
                    supplied.append(f'{key}="fixture"')
        else:
            assert any(kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == 'request'
                       for kw in node.keywords), node.lineno
        if supplied:
            argument = node.args[0]
            end = starts[argument.end_lineno-1] + argument.end_col_offset
            changes.append((end, (', '+', '.join(supplied)).encode()))
            print(module.path.name, node.lineno, supplied)
    changed = data
    for position, added in sorted(changes, reverse=True):
        changed = changed[:position] + added + changed[position:]
    assert module.path.read_bytes() == data
    module.path.write_bytes(changed)
