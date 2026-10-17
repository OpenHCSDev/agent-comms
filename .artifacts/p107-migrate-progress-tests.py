"""Use NRA's syntax index to migrate Python constructor/expectation boundaries.

GoalProgress is already decoded by FieldCodec at JSON entrypoints. Python tests
must construct that value, not depend on frozen records repairing their inputs.
The complete constructor census is retained in the turn/source evidence.
"""
import ast
import json
from pathlib import Path
from nominal_refactor_advisor.ast_tools import module_syntax_index

allowed = {'Goal', 'replace', 'ActiveGoalAction', 'PausedGoalAction', 'BlockedGoalAction',
           'CompletedGoalAction', 'CommsResumeGoalTool', 'action', 'transition', 'prior',
           'BlockedGoalAction if prior is BlockedGoalAction else ActiveGoalAction'}
changes = []
for path in Path('tests').rglob('*.py'):
    raw = path.read_bytes()
    index = module_syntax_index(ast.parse(raw))
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    def span(node):
        return starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset
    edits = []
    for _, call in index.indexed_nodes_of_type(ast.Call):
        for kw in call.keywords:
            if kw.arg != 'progress':
                continue
            literal = isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
            requested = path.name == 'test_goal_resume.py' and isinstance(kw.value, ast.Name) and kw.value.id == 'requested_progress'
            if literal or requested:
                assert ast.unparse(call.func) in allowed, (path, call.lineno, ast.unparse(call.func))
                start, end = span(kw.value)
                edits.append((start, end, b'TextGoalProgress(' + raw[start:end] + b')'))
    for _, compare in index.indexed_nodes_of_type(ast.Compare):
        left = compare.left
        if not (isinstance(left, ast.Attribute) and left.attr == 'progress'):
            continue
        expected = compare.comparators[0]
        if ((isinstance(expected, ast.Constant) and isinstance(expected.value, str))
            or (isinstance(expected, ast.Name) and expected.id == 'requested_progress')):
            start, end = span(left)
            edits.append((start, end, b'str(' + raw[start:end] + b')'))
    if not edits:
        continue
    if any(b'TextGoalProgress(' in replacement for _, _, replacement in edits):
        imports = [node for kind in (ast.Import, ast.ImportFrom)
                   for _, node in index.indexed_nodes_of_type(kind)
                   if not isinstance(node, ast.ImportFrom) or node.module != '__future__']
        first = min(imports, key=lambda node: node.lineno)
        insertion = starts[first.lineno - 1]
        edits.append((insertion, insertion, b'from agent_comms.goals import TextGoalProgress\n'))
    result = raw
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    ast.parse(result)
    changes.append({'path': str(path), 'edits': len(edits)})
    assert path.read_bytes() == raw
    path.write_bytes(result)
Path('.artifacts/p107-progress-migration.json').write_text(json.dumps(changes, indent=2) + '\n')
print(json.dumps(changes, indent=2))
