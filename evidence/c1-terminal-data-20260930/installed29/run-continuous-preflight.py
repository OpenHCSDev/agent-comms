from pathlib import Path
import datetime,hashlib,json,os,subprocess,time
stage=Path('/home/ts/.local/share/agent-comms/runtime-native-source-queue-cohort-20260930')
run=Path('/home/ts/wt/g458g/u04')
run.mkdir(exist_ok=False)
(run/'tmp').mkdir();(run/'proof').mkdir()
metadata=json.loads(Path('/home/ts/wt/g458g/meta/root-receipt.json').read_text())
pins={'core':'970bc527f4ddd9b3bde5522dc671aea11fd27ece','toad':'d3ba4cf330acd4d2eec6cd806fab8113c3a046ec','textual':'6b5895fa0a72aeec2aeaef7206d5debfa0c1803c','driver':'2835a566b7863b9fa44a76dc3b1381eb04df6dd7'}
activation=stage/'activation.json';before=hashlib.sha256(activation.read_bytes()).hexdigest()
assert before=='c354f5e5ad7faff5901b144bf5233ad305d589e0b1d073dc51223eb6f016daa3'
cmd=['script','-q','-e','-f','-T',str(run/'terminal.timing'),'-c',str(stage/'bin/python')+' /home/ts/wt/g458g/continuous-with-menu-preflight.py',str(run/'terminal.ansi')]
env=os.environ.copy();env.update(PATH=str(stage/'bin')+':'+env.get('PATH',''),PYTHONPATH='/home/ts/wt/g458g/dev',TERM='xterm-256color',COLUMNS='160',LINES='44',TMPDIR=str(run/'tmp'),L0A_EVIDENCE=str(run/'proof'),INPUT_VISIBILITY_FIXTURE=str(run/'fixture'),INPUT_VISIBILITY_TERMINAL='1',TOAD_TEST_ATTEMPT='g458g-u04',TOAD_TEST_SOURCE_HEAD=pins['toad'],AGENT_COMMS_ROOT=metadata['root'],AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=metadata['root_id'],AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=metadata['native_package'],AC_NATIVE_COPIED_PACKAGE=metadata['native_package'])
for name in ('DISPLAY','WAYLAND_DISPLAY'):env.pop(name,None)
started=time.monotonic()
with (run/'wrapper.log').open('wb') as log:
 process=subprocess.Popen(cmd,cwd='/home/ts/wt/toad-input-delivery-visibility-20260930/tests',env=env,stdout=log,stderr=subprocess.STDOUT)
 launch={**pins,'pid':process.pid,'argv':cmd,'activation_sha256':before,'private_fixture':str(run/'fixture'),'provider_budget':5,'paid_calls':0,'public_mutations':0,'old_u01_preserved':True,'old_input_replay':False,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'resource_preflight':{'level':'warning','ram_available_gib':15.8,'home_free_gib':11.3,'swap_used_gib':8.7,'authorization':'one bounded serial gate; original no-input failure retained; explicit reviewed driver continuation'}}
 (run/'launch.json').write_text(json.dumps(launch,indent=2)+'\n');print(json.dumps({'started':launch,'run':str(run)}),flush=True)
 try:code=process.wait(timeout=180)
 except subprocess.TimeoutExpired:
  (run/'wrapper-timeout.json').write_text(json.dumps({'pid':process.pid,'at_seconds':time.monotonic()-started,'state':'original-input-disposition-needs-inspection-no-replay'})+'\n')
  print('Owned wrapper wait expired; original process/input must be inspected, no automatic retry.',flush=True)
  raise
receipt={**pins,'exit_code':code,'elapsed_seconds':time.monotonic()-started,'stage':str(stage),'runtime_activation_unchanged':hashlib.sha256(activation.read_bytes()).hexdigest()==before,'manual_replay':False,'old_u01_preserved':True,'old_u02_preserved':True,'public_mutations':0,'paid_calls':0,'gate':'existing actual native+ACP+LinuxDriver firstfork/five original preDelivery/oncechat/cancel; raw source proof review pending'}
(run/'terminal-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
raise SystemExit(code)
