from pathlib import Path
import ast,json,sys
p=Path(sys.argv[1]);root=Path(sys.argv[2]);data=json.loads(p.read_text());targets=set(data['owners']);methods=set()
for path in sorted(root.joinpath('src/agent_comms').rglob('*.py')):
 tree=ast.parse(path.read_text());rel=str(path.relative_to(root))
 for node in ast.walk(tree):
  if isinstance(node,ast.ClassDef) and node.name in targets:
   match=next(d for d in data['owners'][node.name]['declarations'] if d['path']==rel and d['line']==node.lineno)
   match['annotated_fields']=[{'name':ast.unparse(x.target),'type':ast.unparse(x.annotation),'line':x.lineno} for x in node.body if isinstance(x,ast.AnnAssign)]
   match['methods']=[{'name':x.name,'line':x.lineno,'end_line':x.end_lineno,'decisions':[{'line':b.lineno,'expression':ast.unparse(b.test)} for b in ast.walk(x) if isinstance(b,(ast.If,ast.IfExp,ast.While))]} for x in node.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef))]
   methods.update(x['name'] for x in match['methods'] if not x['name'].startswith('__'))
data['method_reference_candidates']=[]
for path in sorted(root.joinpath('src/agent_comms').rglob('*.py')):
 lines=path.read_text().splitlines();tree=ast.parse('\n'.join(lines));rel=str(path.relative_to(root))
 for node in ast.walk(tree):
  if isinstance(node,ast.Call):
   name=node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id if isinstance(node.func,ast.Name) else ''
   if name in methods:data['method_reference_candidates'].append({'path':rel,'line':node.lineno,'name':name,'source':lines[node.lineno-1].strip()})
data['method_reference_limit']='Same-named call sites include unrelated owners. Read and resolve receivers before assigning semantic ownership; this search does not infer runtime targets or certify closure.'
p.write_text(json.dumps(data,indent=2)+'\n');print(json.dumps({'owners':len(targets),'method_candidates':len(data['method_reference_candidates']),'bytes':p.stat().st_size}))
