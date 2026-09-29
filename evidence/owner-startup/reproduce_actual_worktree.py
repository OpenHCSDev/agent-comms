"""No prompt: mirror the failed live declaration in a private isolated launch."""
import os
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from agent_comms.child_process import ObservedProcess
from agent_comms.comms import Comms
from agent_comms.registration import Registration
from agent_comms.runtime import socket_path

receipt=Path(__file__).parent
stage=receipt.parent.parent/'.artifacts'/'actual-worktree'
stage.mkdir(parents=True,exist_ok=True)
config=stage/'pi';config.mkdir(exist_ok=True)
for name in ('models.json','settings.json'):
 source=Path.home()/'.pi'/'agent'/name
 if source.exists():shutil.copyfile(source,config/name)
os.environ['PI_CODING_AGENT_DIR']=str(config.resolve())
for key in ('AGENT_COMMS_AGENT_ARGS','PI_PROMPT','PI_PARENT_ID','PI_TASK','AGENT_COMMS_STARTUP_INPUT_KEY','PI_AGENT_ID'):
 os.environ.pop(key,None)
source=Registration(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza/registry.json'))
original=source.require('openhcs-pr159-viewer-bind-owner')
with tempfile.TemporaryDirectory(prefix='comms-startup-mirror-',dir='/var/tmp') as directory:
 root=Path(directory)/'wire'
 comms=Comms(root)
 root_id=comms.messaging.initialize_private_initial_protocol()
 package=Path('/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent')
 comms.owners.pin_private_nk_launch(root,root_id,package)
 parent=source.require(original.parent)
 comms.threads.register(replace(parent,process_identity=None,active_turn=None))
 comms.threads.register(replace(original,process_identity=None,active_turn=None))
 result=comms.owners.start(original.name)
 owner=comms.registry.require(original.name)
 try:
  end=time.monotonic()+20
  while owner.process_alive and not socket_path(root,result.pid).exists():
   if time.monotonic()>end:raise TimeoutError('Actual owner never published socket')
   time.sleep(.02)
  print('ACTUAL_WORKTREE_STARTUP',owner.process_alive,socket_path(root,result.pid).exists(),flush=True)
 finally:
  if owner.process_alive:ObservedProcess(owner.process_identity).stop_sync()
  for path in (root/'diagnostics').glob('owner-*.log'):
   shutil.copyfile(path,receipt/('actual-worktree-'+path.name))
