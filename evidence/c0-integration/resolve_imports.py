"""One-time combination of disjoint C0 definitions and operation call changes."""
import ast,json,re,subprocess
from pathlib import Path
conflicts=subprocess.check_output(['git','diff','--name-only','--diff-filter=U'],text=True).splitlines()
for name in conflicts:
 path=Path(name)
 if name=='src/agent_comms/operations.py':
  subprocess.run(['git','rm',name],check=True);continue
 if name=='src/agent_comms/__init__.py':
  path.write_text(subprocess.check_output(['git','show',':2:'+name],text=True));continue
 s=path.read_text();s=re.sub(r'<<<<<<<[^\n]*\n(.*?)=======\n(.*?)>>>>>>>[^\n]*\n',lambda m:m[2],s,flags=re.S)
 path.write_text(s)
d=json.loads(Path('evidence/c0-declarations/symbol-owners.json').read_text())
o=json.loads(Path('evidence/c0-operations/caller-map.json').read_text())['symbols']
all_symbols={**d,**o}
for folder in ('src','tests','scripts','benchmarks','tools','stack'):
 for path in Path(folder).rglob('*.py'):
  source=path.read_text()
  source=source.replace('from agent_comms.operations import','from agent_comms.comms import')
  # Rewrite imports structurally, including imported names whose new owner differs.
  tree=ast.parse(source);lines=source.splitlines(keepends=True);starts=[0]
  for line in lines:starts.append(starts[-1]+len(line))
  edits=[]
  for n in ast.walk(tree):
   if not isinstance(n,ast.ImportFrom):continue
   relative=n.level==1 and n.module in ('declarations','operations','comms')
   absolute=n.module in ('agent_comms','agent_comms.declarations','agent_comms.operations','agent_comms.comms')
   if not (relative or absolute):continue
   groups={}
   for alias in n.names:
    module=all_symbols.get(alias.name)
    if module:
     module=('.' if relative else 'agent_comms.')+module
    else:module='.'*n.level+n.module
    groups.setdefault(module,[]).append(alias.name+(' as '+alias.asname if alias.asname else ''))
   replacement=('\n'+' '*n.col_offset).join('from '+module+' import '+', '.join(names) for module,names in groups.items())
   edits.append((starts[n.lineno-1]+n.col_offset,starts[n.end_lineno-1]+n.end_col_offset,replacement))
  for a,b,v in sorted(edits,reverse=True):source=source[:a]+v+source[b:]
  # Retarget explicit module patch hooks to the actual definition owner.
  aliases=re.findall(r'from agent_comms import declarations(?: as (\w+))?',source)
  needed=set()
  for alias in aliases:
   alias=alias or 'declarations'
   def access(m):
    name=m[1];owner=d.get(name,'store_files' if name in ('os','time') else None)
    if owner is None:raise RuntimeError((str(path),alias,name))
    needed.add(owner);return owner+'.'+name
   source=re.sub(r'\b'+alias+r'\.(\w+)',access,source)
  if aliases:
   source=re.sub(r'from agent_comms import declarations(?: as \w+)?',lambda _: 'from agent_comms import '+', '.join(sorted(needed)),source)
  for name,owner in all_symbols.items():
   source=source.replace('agent_comms.declarations.'+name,'agent_comms.'+owner+'.'+name)
  source=source.replace('agent_comms.declarations.os','agent_comms.store_files.os').replace('agent_comms.declarations.time','agent_comms.store_files.time')
  if source!=path.read_text():path.write_text(source)
p=Path('src/agent_comms/__init__.py');s=p.read_text();tree=ast.parse(s);lines=s.splitlines(keepends=True)
for n in reversed(tree.body):
 if isinstance(n,ast.ImportFrom) and n.module in set(o.values()):del lines[n.lineno-1:n.end_lineno]
s=''.join(lines)
for name in o:s=re.sub(r'^    "'+name+r'",\n','',s,flags=re.M)
p.write_text(s)
p=Path('README.md');s=p.read_text().replace('from agent_comms import Thread','from agent_comms.threads import Thread').replace('worktree="/tmp/wt"','worktree=str(Path("~/wt/pr111").expanduser())');p.write_text(s)
p=Path('tests/test_native_source_cursor_certificate.py');s=p.read_text().replace('Thread(name, frozenset({"team"}), str(tmp_path), pid=os.getpid())','Thread(name, frozenset({"team"}), str(tmp_path), pid=os.getpid(), model="fake/fake")');p.write_text(s)
print('Combined import conflicts; preserved operation calls and current declaration destinations.')
