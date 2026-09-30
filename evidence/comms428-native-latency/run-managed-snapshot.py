from pathlib import Path
import importlib.util,subprocess,os,json,time
repo=Path('/home/ts/wt/comms428-native-wait-custody-20260930')
base=Path('/home/ts/.cache/agent-scratch/comms428-native-wait-custody-20260930')
root=base/'managed-snapshot02';root.mkdir(mode=0o700)
for name in ('agent','project','home'): (root/name).mkdir(mode=0o700)
(root/'agent/auth.json').write_text('{}');(root/'agent/models.json').write_text('{}');(root/'agent/settings.json').write_text('{"packages":[]}')
(root/'project/fixture.txt').write_text('before\n')
from agent_comms.comms import Comms
from agent_comms.threads import Thread
c=Comms(root/'wire');rid=c.messaging.initialize_private_initial_protocol();c.registry.declare(Thread('managed-fixture',frozenset({'fixture'}),str(root/'project')))
package=Path('/home/ts/.local/share/agent-comms/native-current-e36a1dde326b7017/node_modules/@earendil-works/pi-coding-agent')
spec=importlib.util.spec_from_file_location('isolation',repo/'stack/test-native-import-rpc.py');isolation=importlib.util.module_from_spec(spec);spec.loader.exec_module(isolation)
env={'HOME':str(root/'home'),'PATH':str(base/'installed/bin')+':/usr/bin','PI_CODING_AGENT_DIR':str(root/'agent'),'PI_PARENT_ID':'fixture-parent','PI_AGENT_ID':'managed-fixture','AGENT_COMMS_THREAD':'managed-fixture','AGENT_COMMS_MANAGED':'1','AGENT_COMMS_ROOT':str(root/'wire'),'AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID':rid,'AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE':str(package),'PI_OFFLINE':'1','NODE_DISABLE_COMPILE_CACHE':'1'}
a=time.monotonic();run=subprocess.run(['/usr/bin/node',str(base/'managed-snapshot-gate.mjs'),str(repo),str(package),str(root)],env=env,cwd=root/'project',capture_output=True,text=True,timeout=30,preexec_fn=isolation.network_denial());(root/'stdout.log').write_text(run.stdout);(root/'stderr.log').write_text(run.stderr);print(json.dumps({'exit':run.returncode,'wall':time.monotonic()-a,'root':str(root)}));print(run.stdout);print(run.stderr[:2000]);assert run.returncode==0
