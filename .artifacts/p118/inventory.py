import ast,json,sys
from pathlib import Path
sys.path.insert(0,'/home/ts/.pi/agent/skills/refactor-audit/scripts')
from audit.repository import Repository
from audit.findings import Package
names={'PiChild','PiSessionChild','PiRpcChannel','Response','StoredEntry','StoredRecord','TranscriptSource','NativeTranscript','DurableTranscript','AgentSettings','ModelRef','ConfigurationData','Configure','GetState','GetAvailableModels','SetModel','SetThinkingLevel','turn','next_event','request','exchange','response','_read_task','deliver_event','turns','completed_turn','turn_events','unique_fields'}
repo=Repository(Path('/home/ts/wt/core-p118-owner')); package=Package.load(repo,sys.argv[1],'src/agent_comms'); sites=[]
for module in package.modules:
 for node in ast.walk(module.tree):
  if names.intersection({getattr(node,'id',''),getattr(node,'attr',''),getattr(node,'name',''),*([a.name for a in node.names] if isinstance(node,(ast.Import,ast.ImportFrom)) else [])}):
   sites.append({'path':module.path,'line':node.lineno,'kind':type(node).__name__,'source':node.name if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) else ast.unparse(node)})
print(json.dumps({'revision':sys.argv[1],'modules':len(package.modules),'omissions':package.unparsed,'sites':sites},indent=2))
