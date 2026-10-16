"""Migrate existing busy-only uses using the NRA-parsed source corpus."""
from pathlib import Path
exec(Path(__file__).with_name('source_sites.py').read_text().split('rev=sys.argv[1]')[0])
for scope in ('src/agent_comms','tests','tools','stack'):
    pkg=Package.load(Working(root),'working',scope);assert not pkg.unparsed
    for m in pkg.modules:
        if m.path=='src/agent_comms/coordination_errors.py':continue
        parents={child:parent for parent in ast.walk(m.tree) for child in ast.iter_child_nodes(parent)}
        edits=[];lines=m.text.splitlines(keepends=True)
        def span(n):return sum(map(len,lines[:n.lineno-1]))+n.col_offset,sum(map(len,lines[:n.end_lineno-1]))+n.end_col_offset
        for n in ast.walk(m.tree):
            if not isinstance(n,ast.Name) or n.id!='CoordinationReadUnavailable':continue
            selected=m.path.startswith('tests/')
            parent=parents.get(n)
            if isinstance(parent,ast.Call) and parent.func is n:selected=True
            if isinstance(parent,ast.ClassDef) and parent.name=='RouteIndexRebuilding':selected=True
            current=parent
            while current is not None:
                if isinstance(current,ast.ExceptHandler) and current.type is not None and n in ast.walk(current.type):selected=True
                if isinstance(current,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DEFERRED_READS' for t in current.targets):selected=True
                current=parents.get(current)
            if selected:edits.append((*span(n),'CoordinationReadBusy'))
        if not edits:continue
        if not any(isinstance(n,ast.ImportFrom) and any(a.name=='CoordinationReadBusy' for a in n.names) for n in ast.walk(m.tree)):
            position=0
            for n in m.tree.body:
                if isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str) or isinstance(n,ast.ImportFrom) and n.module=='__future__':position=sum(map(len,lines[:n.end_lineno]))
                else:break
            edits.append((position,position,'\nfrom agent_comms.coordination_errors import CoordinationReadBusy\n'))
        text=m.text
        for start,end,replacement in sorted(edits,reverse=True):text=text[:start]+replacement+text[end:]
        ast.parse(text);(root/m.path).write_text(text);print(m.path,len(edits))
