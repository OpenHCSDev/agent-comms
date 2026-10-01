from pathlib import Path
import datetime,hashlib,json,os,subprocess,time
stage=Path('/home/ts/.local/share/agent-comms/runtime-source-publication-custody-20260930')
run=Path('/home/ts/wt/g458h/u02')
run.mkdir(exist_ok=False)
(run/'tmp').mkdir();(run/'proof').mkdir()
metadata=json.loads(Path('/home/ts/wt/g458h/meta/root-receipt.json').read_text())
pins={'core':'970bc527f4ddd9b3bde5522dc671aea11fd27ece','toad':'85d45b512cac88bdf5dd8870f41e53768ab3c009','textual':'6b5895fa0a72aeec2aeaef7206d5debfa0c1803c','driver':'214aaacba056cf582c2f4d39387872e25bd9537f'}
activation=stage/'activation.json';before=hashlib.sha256(activation.read_bytes()).hexdigest()
assert before=='48207a4ad51c8008a1011d4d5aa9725cebc1a2d22523ea3ac6417ad7810d7e83'
cmd=['script','-q','-e','-f','-T',str(run/'terminal.timing'),'-c',str(stage/'bin/python')+' /home/ts/wt/g458h/continuous-with-menu-preflight-guarded.py',str(run/'terminal.ansi')]
env=os.environ.copy();env.update(PATH=str(stage/'bin')+':'+env.get('PATH',''),PYTHONPATH='/home/ts/wt/g458g/dev',TERM='xterm-256color',COLUMNS='160',LINES='44',TMPDIR=str(run/'tmp'),L0A_EVIDENCE=str(run/'proof'),INPUT_VISIBILITY_FIXTURE=str(run/'fixture'),INPUT_VISIBILITY_TERMINAL='1',TOAD_TEST_ATTEMPT='g458h-u02',TOAD_TEST_SOURCE_HEAD=pins['toad'],AGENT_COMMS_ROOT=metadata['root'],AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=metadata['root_id'],AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=metadata['native_package'],AC_NATIVE_COPIED_PACKAGE=metadata['native_package'])
for name in ('DISPLAY','WAYLAND_DISPLAY'):env.pop(name,None)
started=time.monotonic()
with (run/'wrapper.log').open('wb') as log:
 process=subprocess.Popen(cmd,cwd='/home/ts/wt/toad-input-delivery-visibility-20260930/tests',env=env,stdout=log,stderr=subprocess.STDOUT)
 launch={**pins,'pid':process.pid,'argv':cmd,'activation_sha256':before,'private_fixture':str(run/'fixture'),'provider_budget':5,'paid_calls':0,'public_mutations':0,'old_u01_preserved':True,'old_input_replay':False,'launcher_correction_sha256':'8f7929b1114c64f8f9f8d8545958fe4758549b98091bb0460417297e782cdd97','started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'resource_preflight':{'level':'warning','ram_available_gib':19.0,'home_free_gib':10.3,'swap_used_gib':8.7,'authorization':'one bounded serial gate; original no-input failure retained; explicit reviewed driver continuation'}}
 (run/'launch.json').write_text(json.dumps(launch,indent=2)+'\n');print(json.dumps({'started':launch,'run':str(run)}),flush=True)
 try:code=process.wait(timeout=180)
 except subprocess.TimeoutExpired:
  (run/'wrapper-timeout.json').write_text(json.dumps({'pid':process.pid,'at_seconds':time.monotonic()-started,'state':'original-input-disposition-needs-inspection-no-replay'})+'\n')
  print('Owned wrapper wait expired; original process/input must be inspected, no automatic retry.',flush=True)
  raise
receipt={**pins,'exit_code':code,'elapsed_seconds':time.monotonic()-started,'stage':str(stage),'runtime_activation_unchanged':hashlib.sha256(activation.read_bytes()).hexdigest()==before,'manual_replay':False,'old_u01_preserved':True,'old_u02_preserved':True,'public_mutations':0,'paid_calls':0,'gate':'existing actual native+ACP+LinuxDriver firstfork/five original preDelivery/oncechat/cancel; raw source proof review pending'}
(run/'terminal-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
raise SystemExit(code)
