"""Compare applied permission bodies with the existing integration base; no imports."""

import ast
import hashlib
import subprocess
from pathlib import Path

ROOT = Path('/home/ts/wt/comms-friction--P17')
SOURCE = ROOT / 'src/agent_comms'


def original(module: str) -> str:
    return subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'HEAD:src/agent_comms/{module}'],
        check=True, capture_output=True, text=True,
    ).stdout


def declaration(text: str, *names: str) -> ast.AST:
    body = ast.parse(text).body
    for name in names:
        node, = [item for item in body
                 if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                 and item.name == name]
        body = node.body
    return node


print('Base:', subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                               check=True, capture_output=True, text=True).stdout.strip())
origin_before = original('input_origin.py')
origin_after = (SOURCE / 'input_origin.py').read_text()
for name in ('capture', '_require_owner', 'require_ingress', 'applies', 'matches_owner'):
    before = declaration(origin_before, 'HumanInputOrigin', name)
    after = declaration(origin_after, 'HumanInputOrigin', name)
    assert [ast.dump(node) for node in before.body] == [ast.dump(node) for node in after.body], name
    print(f'HumanInputOrigin.{name}: original body unchanged')

for name in ('HumanInputOrigin', 'InputProvenance'):
    before = declaration(origin_before, name)
    after = declaration(origin_after, name)
    assert [ast.dump(node) for node in before.body if isinstance(node, ast.AnnAssign)] == [
        ast.dump(node) for node in after.body if isinstance(node, ast.AnnAssign)]
    print(f'{name}: original own field declarations unchanged')

pin_before = declaration(original('task_sources.py'), 'NativeInputConstraintPin', 'require_wording_publication')
pin_after = declaration((SOURCE / 'task_sources.py').read_text(), 'NativeInputConstraintPin', 'require_wording_publication')
assert [ast.dump(node) for node in pin_before.body[4:]] == [ast.dump(node) for node in pin_after.body[1:]]
print('NativeInputConstraintPin: original origin/root/author/recipient/project checks unchanged after lookup')

selection_before = original('registry/registry_document.py')
selection_after = (SOURCE / 'registry/user_thread_lookup.py').read_text()
for name in ('UserThreadLookup', 'FoundUserThread', 'NoUserThread'):
    assert ast.dump(declaration(selection_before, name)) == ast.dump(declaration(selection_after, name)), name
    print(f'{name}: original declaration moved without change')

for module in ('input_origin.py', 'input_ingress.py', 'input_publication_read.py',
               'input_publication_source.py', 'task_sources.py', 'bus.py'):
    raw = (SOURCE / module).read_bytes()
    print(hashlib.sha256(raw).hexdigest(), module)
print('Source preservation only. Imports, MRO, runtime annotations and consumers are not certified.')
