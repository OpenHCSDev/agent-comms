"""One-use, revision-checked consumer migration using NRA's syntax authority.

Exact source edits, not an equivalence proof. Deferred/ambiguous bindings are
reported instead of rewritten. Production ownership was decided before this.
"""
from pathlib import Path
import ast
from collections import Counter, defaultdict
from nominal_refactor_advisor.ast_tools import (
    module_syntax_index, parse_python_module_roots, python_module_path_identities_for_roots,
)

ROOT = Path('/home/ts/wt/core-native-runtime')
COUNTS = Counter()
OPEN = []


def offsets(data):
    result = [0]
    for line in data.splitlines(keepends=True):
        result.append(result[-1] + len(line))
    return result


def span(node, starts):
    return starts[node.lineno-1]+node.col_offset, starts[node.end_lineno-1]+node.end_col_offset


def text(node, starts, data):
    a,b = span(node, starts)
    return data[a:b].decode()


def remove_argument(call, argument, starts, data):
    a,b = span(argument, starts)
    end = span(call, starts)[1]
    following = b
    while following < end and data[following:following+1].isspace():
        following += 1
    if data[following:following+1] == b',':
        return a,following+1,b''
    previous = a-1
    while previous >= 0 and data[previous:previous+1].isspace():
        previous -= 1
    return (previous if data[previous:previous+1] == b',' else a),b,b''


def simple_receiver(node):
    if isinstance(node, ast.Name):
        return True
    return isinstance(node, ast.Attribute) and simple_receiver(node.value)


def call_name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


for phase in ('owner', 'settings', 'binary', 'constructors'):
    roots = (ROOT/'tests', ROOT/'tools')
    modules = parse_python_module_roots(roots, use_parse_cache=False, parse_workers=1)
    parsed = {module.path.resolve() for module in modules}
    omissions = [str(row.path) for row in python_module_path_identities_for_roots(roots)
                 if row.path.resolve() not in parsed]
    if omissions:
        raise RuntimeError(f'Parse omissions before {phase}: {omissions}')
    print('phase', phase, 'modules',len(modules),'omissions []')
    for module in modules:
        path = module.path
        original = path.read_bytes()
        starts = offsets(original)
        index = module_syntax_index(module.module)
        edits = []
        insertions = defaultdict(list)
        if phase == 'owner':
            for _,node in index.indexed_nodes_of_type(ast.Attribute):
                if node.attr == 'native_defaults' and isinstance(node.value, ast.Attribute) and node.value.attr == 'owners':
                    a,b = span(node,starts)
                    edits.append((a,b,(text(node.value.value,starts,original)+'.settings').encode()))
                    COUNTS['root-settings-read/write'] += 1
        for position,node in index.indexed_nodes_of_type(ast.Call):
            name = call_name(node.func)
            if phase == 'settings' and isinstance(node.func, ast.Attribute) and call_name(node.func.value) == 'NativeThreadDefaults':
                if name == 'from_runtime':
                    a,b = span(node,starts)
                    edits.append((a,b,b'NativeThreadDefaults()'))
                    COUNTS['settings-no-runtime'] += 1
                elif name == 'offline' and node.args:
                    edits.append(remove_argument(node,node.args[0],starts,original))
                    COUNTS['offline-no-runtime'] += 1
            if phase == 'binary':
                owns_settings = name == 'NativeThreadDefaults' or (
                    name == 'replace' and node.args and isinstance(node.args[0],ast.Attribute) and node.args[0].attr == 'settings')
                if owns_settings:
                    for keyword in node.keywords:
                        if keyword.arg == 'binary':
                            edits.append(remove_argument(node,keyword,starts,original))
                            COUNTS['deleted-settings-executable'] += 1
            if phase != 'constructors':
                continue
            if name not in ('canonical_agent','CommsAgent','CommsClient'):
                continue
            for keyword in node.keywords:
                if keyword.arg != 'native':
                    continue
                if not node.args or not simple_receiver(node.args[0]):
                    OPEN.append((str(path),node.lineno,'non-simple Comms expression'))
                    continue
                root = text(node.args[0],starts,original)
                value = text(keyword.value,starts,original)
                ancestors = index.ancestor_nodes(position)
                if value != root+'.settings':
                    if any(isinstance(parent,(ast.Lambda,ast.comprehension)) for parent in ancestors):
                        OPEN.append((str(path),node.lineno,'deferred settings assignment'))
                        continue
                    statement = next((parent for parent in reversed(ancestors) if isinstance(parent,ast.stmt)),None)
                    if statement is None or isinstance(statement,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                        OPEN.append((str(path),node.lineno,'no immediate statement'))
                        continue
                    line = starts[statement.lineno-1]
                    indentation = original[line:line+statement.col_offset]
                    insertions[line].append(indentation+(root+'.settings = '+value+'\n').encode())
                edits.append(remove_argument(node,keyword,starts,original))
                COUNTS['constructor-reads-Comms'] += 1
        for at,values in insertions.items():
            edits.append((at,at,b''.join(values)))
        if not edits:
            continue
        changed = original
        previous = len(original)+1
        for a,b,value in sorted(edits,reverse=True):
            if b > previous:
                raise RuntimeError(f'Overlapping migration at {path}:{a}:{b}')
            changed = changed[:a]+value+changed[b:]
            previous = a
        if path.read_bytes() != original:
            raise RuntimeError(f'Source changed during migration: {path}')
        path.write_bytes(changed)
print(dict(COUNTS))
for row in OPEN:
    print('OPEN',*row)
print('LIMITS: native settings on other constructor/method families and embedded Python require semantic reconciliation; this is not behavioral proof.')
