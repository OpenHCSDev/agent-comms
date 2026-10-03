import hashlib,json,os,sys
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.goals import Goal
from agent_comms.goal_states import BlockedGoal
from attachment import ready
root=Path(sys.argv[1]); package=Path(sys.argv[2]); service=Comms(root)
service.registry.declare(Thread('history-author',frozenset(),str(root.parent),process_identity=ProcessIdentity.capture(os.getpid())))
service.registry.declare(Thread('history-recipient',frozenset(),str(root.parent)))
root_id=service.messaging.initialize_private_initial_protocol()
service.messaging.send_initial_cohort('history-author','history-recipient','Original retained outbound; no live recipient.')
service.registry.declare(Thread('retained-owner',frozenset({'retained-tag'}),str(root.parent),model='openai-codex/gpt-6.1-sol',thinking_level='off',goal=Goal('Protected original; no replay','protected-goal',state=BlockedGoal('No replay'))))
service.owners.pin_private_nk_launch(root,root_id,package)
os.environ['PI_CODING_AGENT_DIR']=str(root.parent/'owner-config')
os.environ['AGENT_COMMS_NATIVE_CONFIG_DIR']=str(root.parent/'owner-config')
os.environ['BOUNDARY_CONTROL_CREDENTIAL']='original'
service.owners.start('retained-owner',agent_args=['--offline','--no-tools','--thinking','off'])
owner=service.registry.require('retained-owner'); ready(root,owner)
launch=RetainedOwnerLaunch.capture(owner,service.registry.snapshot())
result={'source_python':sys.executable,'root_id':root_id,'original':FieldCodec.encode(owner),'arguments':list(launch.arguments),'binary':launch.binary,'process':FieldCodec.encode(launch.process),'bus_sha256':hashlib.sha256(service.bus.log.path.read_bytes()).hexdigest()}
(root.parent/'source.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'source_ready':True,'pid':owner.pid,'root_id':root_id}),flush=True)
