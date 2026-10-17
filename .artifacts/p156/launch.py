"""Only execute after Codex ends its stop window. Normal launcher, private HOME."""
import json,os,subprocess
from pathlib import Path
import psutil
from agent_comms.active_route import ActiveRoute,publish_active_route
s=Path('/var/tmp/agent-scratch-p156');r=s/'runtime';base=Path('/home/ts/.local/share/agent-comms/runtimes/toad-74d18ddfb-core-ab3848214-native-70f0512abbcfa4e0-20261017T103214Z')
site=r/'lib/python3.14/site-packages';(site/'zz_baseline_dependencies.pth').write_text(str(base/'lib/python3.14/site-packages')+'\n')
assert (r/'bin/pi-comms-native').is_file()
old=json.loads(Path('/home/ts/.cache/agent-scratch/core-p150-open-tab/ui/launch.json').read_text())
x=psutil.Process(old['xvfb']);assert x.create_time()==old['xvfb_birth']
env=dict(os.environ);env.update(HOME=str(s/'home'),DISPLAY=old['display'],AGENT_COMMS_RUNTIME_ROOT=str(r/'bin'),PATH=str(r/'bin')+':'+os.environ['PATH'])
for k in tuple(env):
    if k.startswith('AGENT_COMMS_') and k not in ('AGENT_COMMS_RUNTIME_ROOT',):env.pop(k)
for k in ('NO_COLOR','TOAD_VALIDATION','PYTHONPATH'):env.pop(k,None)
for k,name in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_STATE_HOME','state'),('XDG_CACHE_HOME','cache')]:
    env[k]=str(s/name);(s/name).mkdir(mode=0o700,exist_ok=True)
p=s/'home/.local/share/agent-comms/current';p.parent.mkdir(parents=True);p.symlink_to(r)
root_id=json.loads((s/'bus_meta.json').read_text())['wire_root_id']
# Use the installed native entrypoint's declared package, unchanged from the configured build.
native=Path('/home/ts/wt/core-claude/stack/.pi-native-70f0512abbcfa4e0/node_modules/@earendil-works/pi-coding-agent')
route=s/'home/.local/state/agent-comms/active-route.json';publish_active_route(ActiveRoute(s,root_id,native),route)
cmd='/home/ts/bin/toad-comms; status=$?; printf "%s\\n" "$status" > '+str(s/'exit-code.txt')+'; exit "$status"'
with (s/'terminal.log').open('wb') as log:
    t=subprocess.Popen(['/usr/local/bin/st','-f','DejaVu Sans Mono:pixelsize=16','-g','150x48','-t','P156 private prefix counts','-e','/usr/bin/env','HOME='+env['HOME'],'/bin/bash','-c',cmd],cwd=s,env=env,stdout=log,stderr=log,start_new_session=True)
receipt=dict(terminal=t.pid,terminal_birth=psutil.Process(t.pid).create_time(),display=env['DISPLAY'],root=str(s),home=env['HOME'],runtime=str(r),route=str(route))
(s/'launch.json').write_text(json.dumps(receipt,indent=2));print(receipt)
