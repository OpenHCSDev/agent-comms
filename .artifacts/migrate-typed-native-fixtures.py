"""NRA-indexed boundary migration. Literal argv becomes a typed value once.

Unknown name/attribute bindings are reported rather than made idempotent in
NativeArguments.parse. Source semantics and actual installed checks are separate.
"""
import ast
from pathlib import Path
from collections import Counter
from nominal_refactor_advisor.ast_tools import module_syntax_index, parse_python_module_roots, python_module_path_identities_for_roots
root = Path('/home/ts/wt/core-native-runtime')
roots = (root/'tests', root/'tools')
modules = parse_python_module_roots(roots, use_parse_cache=False, parse_workers=1)
parsed = {module.path.resolve() for module in modules}
missing = [str(row.path) for row in python_module_path_identities_for_roots(roots) if row.path.resolve() not in parsed]
assert not missing, missing
print('modules',len(modules),'omissions',missing)
counts=Counter()
for module in modules:
    data = module.path.read_bytes()
    starts = [0]
    for line in data.splitlines(keepends=True): starts.append(starts[-1]+len(line))
    def span(node): return starts[node.lineno-1]+node.col_offset, starts[node.end_lineno-1]+node.end_col_offset
    def source(node):
        a,b=span(node); return data[a:b].decode()
    edits=[]
    needs_import=False
    index=module_syntax_index(module.module)
    for _,node in index.indexed_nodes_of_type(ast.Call):
        name = node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
        receiver = node.func.value if isinstance(node.func,ast.Attribute) else None
        is_settings = name=='NativeThreadDefaults' or (name=='offline' and isinstance(receiver,ast.Name) and receiver.id=='NativeThreadDefaults')
        is_replace = name=='replace' and node.args and isinstance(node.args[0],ast.Attribute) and node.args[0].attr=='settings'
        if is_settings or is_replace:
            for kw in node.keywords:
                if kw.arg!='arguments': continue
                value=kw.value
                if isinstance(value,ast.Call) and ((isinstance(value.func,ast.Name) and value.func.id=='NativeArguments') or (isinstance(value.func,ast.Attribute) and isinstance(value.func.value,ast.Name) and value.func.value.id=='NativeArguments')):
                    continue
                if isinstance(value,(ast.Tuple,ast.List,ast.BinOp)) or (isinstance(value,ast.Call) and isinstance(value.func,ast.Name) and value.func.id in {'tuple','list'}):
                    a,b=span(value)
                    edits.append((a,a,b'NativeArguments.parse(')); edits.append((b,b,b')'))
                    needs_import=True; counts['argv-boundary']+=1
                elif isinstance(value,ast.Attribute) and value.attr=='arguments':
                    pass
                else:
                    print('OPEN',module.path,node.lineno,'argument binding',source(value))
        if name=='parse' and isinstance(receiver,ast.Name) and receiver.id=='NativeArguments' and len(node.args)==1:
            value=node.args[0]
            if isinstance(value,ast.Attribute) and value.attr=='arguments':
                a,b=span(node); edits.append((a,b,source(value).encode())); counts['deleted-reparse']+=1
        if name=='rpc' and module.path.name=='test_native_arguments.py':
            a,b=span(node); edits.append((b,b,b'.argv')); counts['native-render-assertion']+=1
    for _,node in index.indexed_nodes_of_type(ast.Attribute):
        if node.attr=='parsed_arguments':
            a,b=span(node); edits.append((a,b,(source(node.value)+'.arguments').encode())); counts['typed-access']+=1
        if node.attr=='isolation_arguments':
            a,b=span(node); edits.append((b,b,b'.argv')); counts['constant-native-projection']+=1
    if needs_import and not any(isinstance(node,ast.ImportFrom) and node.module=='agent_comms.native_arguments' and any(alias.name=='NativeArguments' for alias in node.names) for node in module.module.body):
        at = next((span(node)[0] for node in module.module.body if isinstance(node,(ast.Import,ast.ImportFrom)) and not(isinstance(node,ast.ImportFrom) and node.module=='__future__')),None)
        assert at is not None,module.path
        edits.append((at,at,b'from agent_comms.native_arguments import NativeArguments\n'))
    if not edits: continue
    changed=data
    previous=len(data)+1
    for a,b,new in sorted(edits,reverse=True):
        assert b<=previous,(module.path,a,b,previous)
        changed=changed[:a]+new+changed[b:]; previous=a
    assert module.path.read_bytes()==data
    module.path.write_bytes(changed)
print(dict(counts))
print('LIMITS: embedded Python and unknown variable bindings require semantic migration.')
