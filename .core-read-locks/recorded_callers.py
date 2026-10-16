"""Use the recorded identity declaration for positive-birth constructors."""
import ast
import sys
from pathlib import Path
sys.path.insert(0,'/home/ts/.pi/agent/skills/refactor-audit/scripts')
from audit.findings import Package
exec(Path(__file__).with_name('source_sites.py').read_text().split('rev=sys.argv[1]')[0])
for scope in ('src/agent_comms','tests','tools','stack'):
    pkg=Package.load(Working(root),'working',scope);assert not pkg.unparsed
    for m in pkg.modules:
        nodes=[n.func for n in ast.walk(m.tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='ThreadIncarnation']
        if not nodes:continue
        lines=m.text.splitlines(keepends=True);edits=[]
        def span(n):return sum(map(len,lines[:n.lineno-1]))+n.col_offset,sum(map(len,lines[:n.end_lineno-1]))+n.end_col_offset
        for n in nodes:edits.append((*span(n),'RecordedThreadIncarnation'))
        imported=any(isinstance(n,ast.ImportFrom) and any(a.name=='RecordedThreadIncarnation' for a in n.names) for n in ast.walk(m.tree))
        if not imported:
            position=0
            for n in m.tree.body:
                if isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str) or isinstance(n,ast.ImportFrom) and n.module=='__future__':position=sum(map(len,lines[:n.end_lineno]))
                else:break
            module='.thread_identity' if m.path.startswith('src/agent_comms/') else 'agent_comms.thread_identity'
            edits.append((position,position,f'\nfrom {module} import RecordedThreadIncarnation\n'))
        source=m.text
        for start,end,text in sorted(edits,reverse=True):source=source[:start]+text+source[end:]
        ast.parse(source);(root/m.path).write_text(source);print(m.path,len(nodes))
