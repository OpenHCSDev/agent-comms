"""Relevant source sites from the existing NRA audit parser, not a second parser."""
import ast
import json
import sys
from pathlib import Path
sys.path.insert(0, '/home/ts/.pi/agent/skills/refactor-audit/scripts')
from audit.findings import Package
from audit.repository import Repository
root=Path(__file__).resolve().parents[1]
class Working(Repository):
    def python_files(self, rev, scope):
        return tuple(str(p.relative_to(self.path)) for p in sorted((self.path/scope).rglob('*.py')))
    def source(self, rev, path):
        return (self.path/path).read_text()
terms=('LockedStore','JsonDocument','RegistryCache','RegistryRevision','CoordinationReadUnavailable',
       'CoreError','UnavailableCoreError','FailedCoreError','ReadLedger','ReadMask','ReadDocument',
       'ClaimTransition','AcquiringTransition','ReleasingTransition','ClaimOwner','JsonValue',
       'ThreadIncarnation','sender_lookup','_read_unlocked','_decode','_publish_unlocked')
rev=sys.argv[1]
repo=Working(root) if rev=='working' else Repository(root)
counts={};sites=[];omitted=[]
for scope in ('src/agent_comms','tests','tools','stack'):
    pkg=Package.load(repo,rev,scope)
    counts[scope]=len(pkg.modules);omitted.extend(pkg.unparsed)
    for m in pkg.modules:
        for n in ast.walk(m.tree):
            if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                signature=ast.get_source_segment(m.text,n).splitlines()[0]
                if any(t in signature or t in ast.unparse(n.args) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) else t in signature for t in terms):
                    sites.append((m.path,n.lineno,type(n).__name__,signature))
            elif isinstance(n,(ast.Import,ast.ImportFrom,ast.Call,ast.Assign,ast.AnnAssign,ast.If,ast.ExceptHandler)):
                text=ast.unparse(n)
                first=text.splitlines()[0]
                if any(t in first for t in terms):sites.append((m.path,n.lineno,type(n).__name__,first))
print(json.dumps({'revision':rev,'modules':counts,'omissions':list(map(str,omitted)),'sites':sites},indent=2))
assert not omitted
