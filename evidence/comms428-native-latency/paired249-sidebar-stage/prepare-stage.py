from pathlib import Path
import json, os, subprocess, sys, hashlib
core, toad, stage_name = sys.argv[1:]
assert len(core)==40 and len(toad)==40
base=Path('/home/ts/.cache/agent-scratch/sidebar-native-custody-stage-20260930')
stage=Path('/home/ts/.local/share/agent-comms')/stage_name
assert not stage.exists(), 'Never repair/reinstall an existing immutable candidate'
rows=(base/'baseline-68-requirements.txt').read_text().splitlines()
assert len(rows)==68
changes={'agent-comms':core,'batrachian-toad':toad}
new=[]
for row in rows:
 name=row.split(' @ ')[0]
 if name in changes:
  repo='agent-comms' if name=='agent-comms' else 'toad'
  row=f'{name} @ git+https://github.com/OpenHCSDev/{repo}.git@{changes[name]}'
 new.append(row)
req=base/'paired-requirements.txt';req.write_text('\n'.join(new)+'\n')
with (base/'paired-build.log').open('w') as log:
 subprocess.run(['uv','venv','--python','/home/ts/.local/share/agent-comms/runtime-all-merged-20260930/bin/python',str(stage)],check=True,stdout=log,stderr=log)
 subprocess.run(['uv','pip','install','--python',str(stage/'bin/python'),'-r',str(req)],check=True,stdout=log,stderr=log)
 subprocess.run(['uv','pip','check','--python',str(stage/'bin/python')],check=True,stdout=log,stderr=log)
freeze=subprocess.check_output(['uv','pip','freeze','--python',str(stage/'bin/python')],text=True)
(base/'paired-installed-freeze.txt').write_text(freeze)
assert sorted(freeze.splitlines())==sorted(new), 'Frozen dependency cohort changed'
print(json.dumps({'stage':str(stage),'core':core,'toad':toad,'packages':68,'native_package':'/home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/node_modules/@earendil-works/pi-coding-agent','resolver':'normal uv pip install exact frozen requirements; no source override','public_mutations':0}),flush=True)
