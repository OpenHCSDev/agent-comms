"""P82 source evidence using the existing refactor-audit parser, not binding proof."""
import ast,json,sys
from pathlib import Path
sys.path.insert(0,'/home/ts/.pi/agent/skills/refactor-audit/scripts')
from audit.findings import Package
from audit.repository import Repository
package=Package.load(Repository(Path.cwd()),'HEAD','src/agent_comms')
symbols={'WakeDecision','CommittedDelivery','initial_assignment','assignment_decisions','_expected_assignments','_receipt_matches','SourceCoverage','_selected_inputs','WakePolicy','initial_state','MessageAudience'}
classes=[];sites=[]
for module in package.modules:
 for node in ast.walk(module.tree):
  if isinstance(node,ast.ClassDef):classes.append([str(module.path),node.lineno,node.name,[ast.unparse(b) for b in node.bases]])
  if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef,ast.Call,ast.Attribute,ast.Name,ast.Import,ast.ImportFrom,ast.Assign,ast.AnnAssign,ast.If,ast.Compare)):
   text=ast.unparse(node)
   if any(symbol in text for symbol in symbols):sites.append([str(module.path),node.lineno,type(node).__name__,text[:4000]])
Path(sys.argv[1]).write_text(json.dumps({'modules':len(package.modules),'classes':classes,'sites':sites,'parse_omissions':package.unparsed,'limits':'Attribute sites are source leads, not proved dynamic dispatch.'},indent=2))
print('modules',len(package.modules),'classes',len(classes),'sites',len(sites),'omissions',package.unparsed)
