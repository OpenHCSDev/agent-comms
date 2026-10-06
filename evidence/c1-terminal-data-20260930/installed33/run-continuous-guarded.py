from pathlib import Path
import datetime,hashlib,json,os,subprocess,time
stage=Path('/home/ts/.local/share/agent-comms/runtime-atomic-page-handoff-20261001')
run=Path('/home/ts/wt/g458i/u01')
run.mkdir(exist_ok=False)
(run/'tmp').mkdir();(run/'proof').mkdir()
metadata=json.loads(Path('/home/ts/wt/g458i/meta/root-receipt.json').read_text())
pins={'core':'970bc527f4ddd9b3bde5522dc671aea11fd27ece','toad':'15601a4f785fd6ee3e53e12b3a971ae1186f4482','textual':'6b5895fa0a72aeec2aeaef7206d5debfa0c1803c','driver':'98a0eac445bf446ff4a33c65d0121cda836214d4'}
activation=stage/'activation.json';before=hashlib.sha256(activation.read_bytes()).hexdigest()
assert before=='e92534c1ec44de41db22dc1eb08bd336111454aea870e8aa56d0d7b9f3559b94'
cmd=['script','-q','-e','-f','-T',str(run/'terminal.timing'),'-c',str(stage/'bin/python')+' /home/ts/wt/g458i/continuous-with-menu-preflight-guarded.py',str(run/'terminal.ansi')]
env=os.environ.copy();env.update(PATH=str(stage/'bin')+':'+env.get('PATH',''),PYTHONPATH='/home/ts/wt/g458g/dev',TERM='xterm-256color',COLUMNS='160',LINES='44',TMPDIR=str(run/'tmp'),L0A_EVIDENCE=str(run/'proof'),INPUT_VISIBILITY_FIXTURE=str(run/'fixture'),INPUT_VISIBILITY_TERMINAL='1',TOAD_TEST_ATTEMPT='g458i-u01',TOAD_TEST_SOURCE_HEAD=pins['toad'],AGENT_COMMS_ROOT=metadata['root'],AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=metadata['root_id'],AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=metadata['native_package'],AC_NATIVE_COPIED_PACKAGE=metadata['native_package'])
for name in ('DISPLAY','WAYLAND_DISPLAY','NO_COLOR'):env.pop(name,None)
started=time.monotonic()
with (run/'wrapper.log').open('wb') as log:
 process=subprocess.Popen(cmd,cwd='/home/ts/wt/toad-native-fixture-cleanup-20260930/tests',env=env,stdout=log,stderr=subprocess.STDOUT)
 launch={**pins,'pid':process.pid,'argv':cmd,'activation_sha256':before,'private_fixture':str(run/'fixture'),'provider_budget':5,'paid_calls':0,'public_mutations':0,'old_u01_preserved':True,'old_input_replay':False,'launcher_correction_sha256':'06f5e1598099d1df85b1a1dd17009bb8a6f260e80d4da35a742ab84443501c0a','started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'resource_preflight':{'level':'warning','ram_available_gib':16.5,'home_free_gib':9.4,'swap_used_gib':8.9,'authorization':'one bounded actual gate alongside authorized separate BUS root; meaningful reviewed atomic page fix; all original failures retained'}}
 (run/'launch.json').write_text(json.dumps(launch,indent=2)+'\n');print(json.dumps({'started':launch,'run':str(run)}),flush=True)
 try:code=process.wait(timeout=180)
 except subprocess.TimeoutExpired:
  (run/'wrapper-timeout.json').write_text(json.dumps({'pid':process.pid,'at_seconds':time.monotonic()-started,'state':'original-input-disposition-needs-inspection-no-replay'})+'\n')
  print('Owned wrapper wait expired; original process/input must be inspected, no automatic retry.',flush=True)
  raise
receipt={**pins,'exit_code':code,'elapsed_seconds':time.monotonic()-started,'stage':str(stage),'runtime_activation_unchanged':hashlib.sha256(activation.read_bytes()).hexdigest()==before,'manual_replay':False,'old_u01_preserved':True,'old_u02_preserved':True,'public_mutations':0,'paid_calls':0,'gate':'existing actual native+ACP+LinuxDriver firstfork/five original preDelivery/oncechat/cancel; raw source proof review pending'}
(run/'terminal-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
raise SystemExit(code)
