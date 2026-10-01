from pathlib import Path
import datetime,hashlib,json,os,subprocess,time
stage=Path('/home/ts/.local/share/agent-comms/runtime-native-applied-cohort-20261001')
run=Path('/home/ts/wt/g458j/u02');run.mkdir(exist_ok=False)
(run/'tmp').mkdir();(run/'proof').mkdir()
activation=stage/'activation.json';before=hashlib.sha256(activation.read_bytes()).hexdigest()
assert before=='f03e2370464094408d2a8cd3eb90966d55f8dcb6c75829e90cd86ab34ecfb043'
metadata=json.loads(Path('/home/ts/wt/g458j/meta/root-receipt.json').read_text())
pins=json.loads(activation.read_text())['pins']
cmd=['script','-q','-e','-f','-T',str(run/'terminal.timing'),'-c',str(stage/'bin/python')+' /home/ts/wt/g458j/tools/continuous-retained.py',str(run/'terminal.ansi')]
env=os.environ.copy();env.update(PATH=str(stage/'bin')+':'+env.get('PATH',''),PYTHONPATH='/home/ts/wt/g458g/dev',TERM='xterm-256color',COLUMNS='160',LINES='44',TMPDIR=str(run/'tmp'),L0A_EVIDENCE=str(run/'proof'),INPUT_VISIBILITY_FIXTURE=str(run/'fixture'),INPUT_VISIBILITY_TERMINAL='1',TOAD_TEST_ATTEMPT='g458j-u02',TOAD_TEST_SOURCE_HEAD=pins['batrachian-toad'],AGENT_COMMS_ROOT=metadata['root'],AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=metadata['root_id'],AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=metadata['native_package'],AC_NATIVE_COPIED_PACKAGE=metadata['native_package'],G458_ORIGINAL_PYTHON=metadata['original_python'],G458_ORIGINAL_ROOT=metadata['original_root'])
for name in ('DISPLAY','WAYLAND_DISPLAY','NO_COLOR','PI_TASK','PI_PROMPT','PI_PARENT_ID','PI_AGENT_ID','AGENT_COMMS_THREAD','AGENT_COMMS_STARTUP_INPUT_KEY'):env.pop(name,None)
started=time.monotonic()
with (run/'wrapper.log').open('wb') as log:
 process=subprocess.Popen(cmd,cwd='/home/ts/wt/toad-native-fixture-cleanup-20260930/tests',env=env,stdout=log,stderr=subprocess.STDOUT)
 launch={'pins':pins,'wrapper_pid':os.getpid(),'script_pid':process.pid,'argv':cmd,'activation_sha256':before,'private_fixture':str(run/'fixture'),'provider_budget':5,'paid_calls':0,'public_mutations':0,'old_input_replay':False,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_history_bytes':41270331,'resource_preflight':{'level':'warning','ram_available_gib':20.6,'home_free_gib':24.8,'swap_used_gib':12.9,'authorization':'one actual retained native lane alongside separate BUS root'}}
 (run/'launch.json').write_text(json.dumps(launch,indent=2)+'\n');print(json.dumps({'started':launch,'run':str(run)}),flush=True)
 try:code=process.wait(timeout=240)
 except subprocess.TimeoutExpired:
  (run/'wrapper-timeout.json').write_text(json.dumps({'pid':process.pid,'at_seconds':time.monotonic()-started,'state':'original-input-disposition-needs-inspection-no-replay'})+'\n')
  print('Owned observation boundary expired; inspect original processes/inputs, no automatic replay.',flush=True);raise
receipt={'pins':pins,'exit_code':code,'elapsed_seconds':time.monotonic()-started,'stage':str(stage),'runtime_activation_unchanged':hashlib.sha256(activation.read_bytes()).hexdigest()==before,'manual_replay':False,'public_mutations':0,'paid_calls':0,'gate':'actual retained SDK41MB+native+ACP+LinuxDriver five physicalEnter/fork/queue/oncechat/cancel+stationaryReady; raw scope review pending'}
(run/'terminal-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
raise SystemExit(code)
