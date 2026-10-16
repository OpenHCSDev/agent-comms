"""Keep SQLite failure translation at the already existing read boundaries."""
from pathlib import Path
exec(Path(__file__).with_name('source_sites.py').read_text().split('rev=sys.argv[1]')[0])
pkg=Package.load(Working(root),'working','src/agent_comms');assert not pkg.unparsed
for m in pkg.modules:
    edits=[];lines=m.text.splitlines(keepends=True)
    for node in ast.walk(m.tree):
        if not isinstance(node,ast.ExceptHandler) or node.type is None:continue
        if ast.unparse(node.type)!='sqlite3.OperationalError':continue
        if not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='raise_from' and isinstance(n.func.value,ast.Name) and n.func.value.id=='CoordinationReadUnavailable' for n in ast.walk(node)):continue
        n=node.type;edits.append((sum(map(len,lines[:n.lineno-1]))+n.col_offset,sum(map(len,lines[:n.end_lineno-1]))+n.end_col_offset,'sqlite3.DatabaseError'))
    if not edits:continue
    text=m.text
    for start,end,replacement in sorted(edits,reverse=True):text=text[:start]+replacement+text[end:]
    ast.parse(text);(root/m.path).write_text(text);print(m.path,len(edits))
