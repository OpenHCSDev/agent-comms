from pathlib import Path
import ast, hashlib, json, re, subprocess, sys
root=Path(sys.argv[1]); target=Path(sys.argv[2]); symbols=sys.argv[3:]
files=sorted([*root.joinpath('src/agent_comms').rglob('*.py'), *root.joinpath('stack').glob('*.mjs'), *root.joinpath('stack').glob('patch-native-*.py')])
result={'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'scope':'All authored src/agent_comms Python plus stack/*.mjs and stack/patch-native-*.py; tests, compiled dependencies and private journals excluded. Lexical consumers are candidates, not resolved call targets.','files':{},'owners':{s:{'declarations':[],'references':[]} for s in symbols},'parse_errors':[]}
for path in files:
 text=path.read_text(); rel=str(path.relative_to(root)); lines=text.splitlines(); scopes=[]
 result['files'][rel]=hashlib.sha256(text.encode()).hexdigest()
 if path.suffix=='.py':
  try: tree=ast.parse(text)
  except SyntaxError as e: result['parse_errors'].append({'path':rel,'error':str(e)}); continue
  def visit(node, parents=()):
   if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
    name='.'.join((*parents,node.name)); scopes.append((node.lineno,node.end_lineno,name))
    if isinstance(node,ast.ClassDef) and node.name in result['owners']:
     result['owners'][node.name]['declarations'].append({'path':rel,'line':node.lineno,'name':name,'bases':[ast.unparse(x) for x in node.bases]})
    parents=(*parents,node.name)
   for child in ast.iter_child_nodes(node): visit(child,parents)
  visit(tree)
 else:
  for n,line in enumerate(lines,1):
   match=re.search(r'\bclass\s+(\w+)',line)
   if match and match[1] in result['owners']:result['owners'][match[1]]['declarations'].append({'path':rel,'line':n,'name':match[1]})
 for s in symbols:
  for n,line in enumerate(lines,1):
   if re.search(r'\b'+re.escape(s)+r'\b',line):
    enclosing=[x for x in scopes if x[0]<=n<=x[1]]
    scope=min(enclosing,key=lambda x:x[1]-x[0])[2] if enclosing else '<module>'
    result['owners'][s]['references'].append({'path':rel,'line':n,'scope':scope,'source':line.strip()})
for owner in result['owners'].values():owner['declaration_count']=len(owner['declarations'])
target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'head':result['head'],'files':len(files),'parse_errors':result['parse_errors'],'declarations':{s:x['declaration_count'] for s,x in result['owners'].items()},'path':str(target),'bytes':target.stat().st_size}))
