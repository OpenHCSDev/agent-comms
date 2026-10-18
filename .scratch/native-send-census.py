"""Native send ownership evidence using NRA's existing syntax/family owners."""
import ast, json, sys
from pathlib import Path
from nominal_refactor_advisor.ast_tools import SourceModule, module_syntax_index
from nominal_refactor_advisor.class_index import build_class_family_index
root=Path('/home/ts/wt/core-doctrine-segment')
modules=[]; classes=[]; sites=[]; omissions=[]
terms=('NativeAdmission','sent_owner_admission_generation','NativeInputRecord','NativeRuntimeInput','PrivateSendAdmission','NativeSendStage','OwnedSendAdmission','FollowingInputs','bind_turn_input','send_fenced_prompt','prompt_boundary','record_admission','recovery_session_files','require_release')
for scope in ('src/agent_comms','tests','tools/cutover'):
 for path in (root/scope).rglob('*.py'):
  try:
   source=path.read_text()
   parsed=SourceModule.from_source_path(path,source).parse()
  except Exception as error: omissions.append((str(path),str(error))); continue
  syntax=module_syntax_index(parsed.module)
  for _,node in syntax.indexed_nodes_of_type(ast.ClassDef):
   classes.append((str(path.relative_to(root)),node.lineno,node.name,[ast.unparse(base) for base in node.bases]))
  selected=any(term in source for term in terms)
  if not selected: continue
  modules.append(parsed)
  for kind in (ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef,ast.AnnAssign,ast.Assign,ast.ImportFrom,ast.Attribute,ast.Call,ast.Compare):
   for _,node in syntax.indexed_nodes_of_type(kind):
    text=ast.unparse(node)
    if kind in (ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef): text=text.split('\n')[0]
    if kind in (ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef,ast.ImportFrom) or any(term in text for term in terms): sites.append((str(path.relative_to(root)),node.lineno,kind.__name__,text))
family=build_class_family_index(modules)
print(json.dumps({'head':sys.argv[1],'roots':['src/agent_comms','tests','tools/cutover'],'class_rows':classes,'selected_modules':len(modules),'sites':sites,'parse_omissions':omissions,'family_index':type(family).__name__,'dynamic_limits':'Syntax and family resolution do not prove runtime aliases, decorator effects, MRO or behavior.'},indent=2))
