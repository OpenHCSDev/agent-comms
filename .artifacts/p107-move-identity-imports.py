"""Migrate all direct imports to the existing identity owner's declarations."""
import ast
import json
from pathlib import Path
from nominal_refactor_advisor.ast_tools import module_syntax_index

changed = []
for root in ('src', 'tests', 'tools'):
    for path in Path(root).rglob('*.py'):
        raw = path.read_bytes()
        index = module_syntax_index(ast.parse(raw))
        lines = raw.splitlines(keepends=True)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))
        edits = []
        for _, node in index.indexed_nodes_of_type(ast.ImportFrom):
            if node.module not in ('goals', 'agent_comms.goals'):
                continue
            moved = [item for item in node.names if item.name in {'GoalRef', 'GoalRevision'}]
            if not moved:
                continue
            kept = [item for item in node.names if item not in moved]
            owner = 'goal_attempt_identity' if node.module == 'goals' else 'agent_comms.goal_attempt_identity'
            declarations = ([ast.ImportFrom(module=node.module, names=kept, level=node.level)] if kept else [])
            declarations.append(ast.ImportFrom(module=owner, names=moved, level=node.level))
            replacement = ('\n' + ' ' * node.col_offset).join(ast.unparse(item) for item in declarations).encode()
            start = starts[node.lineno - 1] + node.col_offset
            end = starts[node.end_lineno - 1] + node.end_col_offset
            edits.append((start, end, replacement))
        if edits:
            result = raw
            for start, end, replacement in sorted(edits, reverse=True):
                result = result[:start] + replacement + result[end:]
            ast.parse(result)
            assert path.read_bytes() == raw
            path.write_bytes(result)
            changed.append(str(path))
Path('.artifacts/p107-identity-import-migration.json').write_text(json.dumps(changed, indent=2) + '\n')
print('\n'.join(changed))
