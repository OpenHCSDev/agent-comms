import json
from pathlib import Path
r=Path.cwd();ops=[]
for f in ('configure.py','rule_gate.py'):
 ops.append(dict(operation='remove_import_bindings',file_path=str(r/'src/agent_comms/cli_verbs'/f),bound_names=['CliCommand']))
Path('.verification/viewed-object-repair/imports.plan.json').write_text(json.dumps({'recipes':[{'recipe_id':'remove-the-replaced-command-import','operations':ops}]},indent=2))
