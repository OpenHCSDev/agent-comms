"""NRA ownership evidence for the existing ratchet and its consumers."""
import ast,json,subprocess
from pathlib import Path
from nominal_refactor_advisor.ast_tools import SourceModule,module_syntax_index
repo=Path('/home/ts/wt/core-doctrine-segment');p=repo/'src/agent_comms/debt_ratchet.py'
versions={}
for label,text in [('before',subprocess.check_output(['git','-C',str(repo),'show','origin/main:src/agent_comms/debt_ratchet.py'],text=True)),('after',p.read_text())]:
 tree=SourceModule.from_source_path(p,text).parse().module;s=module_syntax_index(tree)
 versions[label]={'classes':[{'name':n.name,'line':n.lineno,'bases':[ast.unparse(b) for b in n.bases]} for _,n in s.indexed_nodes_of_type(ast.ClassDef)],'functions':[{'name':n.name,'line':n.lineno} for _,n in s.indexed_nodes_of_type(ast.FunctionDef)]}
refs=[];errors=[];count=0
names={'Measure','PerFileOccurrenceMeasure','CleanChangedLines','Comparison','ChangedSource'}
for root in (repo/'src',Path('/home/ts/wt/core-doctrine-segment/.scratch/p64/audit-dependency/refactor_audit')):
 for path in root.rglob('*.py'):
  try:tree=SourceModule.from_source_path(path,path.read_text()).parse().module
  except (SyntaxError,UnicodeError) as e:errors.append([str(path),str(e)]);continue
  count+=1;s=module_syntax_index(tree)
  for i,n in s.indexed_nodes_of_type(ast.Name):
   if n.id in names:refs.append({'path':str(path),'line':n.lineno,'name':n.id,'context':type(n.ctx).__name__,'function':s.enclosing_function_name(i)})
  for _,n in s.indexed_nodes_of_type(ast.Attribute):
   if n.attr in {'increased','failed','introduced'}:refs.append({'path':str(path),'line':n.lineno,'name':n.attr,'context':type(n.ctx).__name__})
print(json.dumps({'modules':count,'parse_errors':errors,'versions':versions,'references':refs,'limits':'Syntax references do not prove dynamic alias resolution. Class registration and actual CLI status were exercised separately.'},indent=2))
