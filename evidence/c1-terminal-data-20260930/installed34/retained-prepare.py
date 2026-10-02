"""Existing private fixture prepare_state; original SDK fork and Model only."""
import asyncio,hashlib,json,os,shutil,sys,time
from dataclasses import replace
from pathlib import Path
from agent_comms.native_fork import ForkSessionHelper,ForkSessionRequest
from agent_comms.child_process import BoundedRun
from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileRevision
from agent_comms.active_route import resolve_comms_route
sys.path.insert(0,'/home/ts/wt/comms-c1-terminal-data-20260930/tools/cutover')
from original_owner_capture import CurrentTypedCapture
SOURCE=Path('/home/ts/wt/toad-workspace-growing-end-continuity-20260930/.artifacts/private-warm-scroll-236-01/native-forks/sessions/--home-ts-wt-toad-workspace-growing-end-continuity-20260930-.artifacts-private-warm-scroll-236-01-project--/2026-09-30T12-41-27-530Z_01a0f255-8aeb-754e-9e82-dc119cf460b0.jsonl')
SOURCE_SHA='fc5a6209efae51c1cb82833b1e296ebeeefe9e28b180f5a02d70a2d0928084e0'
CAPTURE=None
BEFORE=None

def source_hash():
 with SOURCE.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

async def prepare(comms,project,requests,entered,release,hold_next):
 global CAPTURE,BEFORE
 evidence=Path(os.environ['L0A_EVIDENCE']);began=time.monotonic()
 # The public source is acquired using its authentic current producer, read only.
 source_python=Path(os.environ['G458_ORIGINAL_PYTHON'])
 root=Path(os.environ['G458_ORIGINAL_ROOT'])
 CAPTURE=await asyncio.to_thread(CurrentTypedCapture(root,source_python).read,'nra-architecture')
 selected=CAPTURE.source.model
 assert selected and '/' in selected
 package=Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
 original_env=dict(CAPTURE.retained.environment)
 for key in ('NODE_OPTIONS','NODE_PATH','NODE_COMPILE_CACHE'):original_env.pop(key,None)
 original_env.update(PI_OFFLINE='1',NODE_DISABLE_COMPILE_CACHE='1')
 directory=await BoundedRun.run(('node','--no-global-search-paths','--import',str(package/'dist/agent-comms-import-fence.mjs'),
  '--input-type=module','--eval',Path('/home/ts/wt/g458j/tools/native-config-directory.mjs').read_text(),str(package)),
  timeout=10,cwd=project,env=original_env)
 assert directory.outcome.successful,(directory.stdout,directory.stderr)
 config=Path(json.loads(directory.stdout)['agentDir'])
 private=project.parent/'capabilities';private.mkdir(mode=0o700)
 (private/'auth.json').write_text('{}');os.chmod(private/'auth.json',0o600)
 for name in ('models.json','models-store.json'):
  original=config/name;target=private/name
  if original.is_file():shutil.copyfile(original,target)
  else:target.write_text('{}')
  os.chmod(target,0o600)
 provider,model_id=selected.split('/',1)
 package=Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
 env=dict(os.environ);env.update(PI_OFFLINE='1',NODE_DISABLE_COMPILE_CACHE='1')
 for key in ('NODE_OPTIONS','NODE_PATH','NODE_COMPILE_CACHE'):env.pop(key,None)
 outcome=await BoundedRun.run(('node','--no-global-search-paths','--import',str(package/'dist/agent-comms-import-fence.mjs'),
  '--input-type=module','--eval',Path('/home/ts/wt/g458j/tools/model-capabilities.mjs').read_text(),str(package),str(private),provider,model_id),
  timeout=10,cwd=project,env=env)
 assert outcome.outcome.successful,(outcome.stdout,outcome.stderr)
 model=json.loads(outcome.stdout)
 # Controlled provider declaration inherits capability fields from the real SDK
 # Model; it alone substitutes provider I/O in the existing whole journey.
 fixture_config=Path(os.environ['PI_CODING_AGENT_DIR'])/'models.json'
 document=json.loads(fixture_config.read_text())
 fixture_model=document['providers']['selected-offline']['models'][0]
 fixture_model.update(contextWindow=model['contextWindow'],maxTokens=model['maxTokens'])
 fixture_config.write_text(json.dumps(document))
 BEFORE=FileRevision.from_stat(SOURCE.stat());assert BEFORE.size==41270331 and source_hash()==SOURCE_SHA
 identity=await ForkSessionHelper.run(ForkSessionRequest(str(package),str(SOURCE),str(project)),cwd=project,
  env=dict(os.environ,PI_CODING_AGENT_DIR=str(project.parent/'native-forks')))
 assert Path(identity.session_file).is_relative_to(project.parent)
 original=comms.registry.require('beta')
 declared=comms.registry.declare(replace(original,session_file=identity.session_file))
 assert declared.session_file==identity.session_file
 assert source_hash()==SOURCE_SHA
 (evidence/'retained-source-preparation.json').write_text(json.dumps({
  'source':str(SOURCE),'source_bytes':BEFORE.size,'source_sha256':SOURCE_SHA,
  'original_owner':FieldCodec.encode(CAPTURE.source.process_identity),
  'selected_model':model,'fork_identity':FieldCodec.encode(identity),
  'declared_thread':FieldCodec.encode(declared),'elapsed_seconds':time.monotonic()-began,
  'provider_posts':len(requests),'original_inputs_replayed':0,'source_unchanged':True},indent=2)+'\n')
 assert not requests
 print('RETAINED_ORIGINAL_SDK_FORK_READY',BEFORE.size,model['contextWindow'],model['maxTokens'],flush=True)

def verify_source():
 assert FileRevision.from_stat(SOURCE.stat())==BEFORE and source_hash()==SOURCE_SHA
 CAPTURE.require_current()
 return {'path':str(SOURCE),'bytes':BEFORE.size,'sha256':SOURCE_SHA,'original_source_and_owner_unchanged':True}
