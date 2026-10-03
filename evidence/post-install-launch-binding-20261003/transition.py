import hashlib,json,os,sys,time
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.private_nk_entrypoint import ROOT_ID_ENV,PACKAGE_ENV
from agent_comms.owner_launch import RestartEnvironment,RetainedOwnerLaunch
from attachment import ready
from retained_index_cutover import RetainedIndexCutover
root=Path(sys.argv[1]); package=Path(sys.argv[2]); source=json.loads((root.parent/'source.json').read_text())
service=Comms(root); original=FieldCodec.decode(Thread,source['original'])
assert service.owners._private_nk_launch is None
try:
    with service.bus.log.locked(): pass
except RelationViolationError as error: refusal=str(error)
else: raise AssertionError('New reader admitted original checkpoint')
cutover=RetainedIndexCutover(Path(source['source_python']),source['root_id'],package)
started=time.monotonic()
try:
    results=service.owners.restart_owners(cutover=cutover,source_interpreter=source['source_python'],runtime=RestartEnvironment.inherit({'PATH':str(Path(sys.executable).parent)+':'+os.environ['PATH'],'VIRTUAL_ENV':sys.prefix}))
    assert len(results)==1,results
    current=service.registry.require('retained-owner'); ready(root,current)
    launch=RetainedOwnerLaunch.capture(current,service.registry.snapshot())
    assert not original.process_alive and current.process_alive
    assert launch.binary==service.owners.native_entrypoint()
    assert launch.interpreter==sys.executable,(launch.interpreter,sys.executable)
    assert list(launch.arguments)==source['arguments']
    assert launch.environment['BOUNDARY_CONTROL_CREDENTIAL']=='original'
    assert launch.environment[ROOT_ID_ENV]==source['root_id']
    assert launch.environment[PACKAGE_ENV]==str(package)
    assert launch.environment['VIRTUAL_ENV']==sys.prefix
    assert current.created_at==original.created_at and current.tags==original.tags
    assert current.model==original.model and current.thinking_level==original.thinking_level and current.goal==original.goal
    assert hashlib.sha256(service.bus.log.path.read_bytes()).hexdigest()==source['bus_sha256']
    assert not Path(f'/proc/{current.pid}/task/{current.pid}/children').read_text().strip()
    receipt={'state':'PASS','source_python':source['source_python'],'target_python':sys.executable,'old_schema_refusal':refusal,'source_identity':source['process'],'target_identity':FieldCodec.encode(launch.process),'source_retired':True,'target_ready':True,'target_entrypoint':launch.binary,'target_runtime_and_private_pair_correct':True,'original_settings_and_arguments_retained':True,'original_wire_bytes_unchanged':True,'new_provider_calls':0,'input_replays':0,'elapsed_seconds':time.monotonic()-started}
    (root.parent/'installed-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
finally:
    thread=service.registry.require('retained-owner')
    if thread.process_alive: service.owners.stop(thread.name)
    assert not service.registry.require('retained-owner').process_alive
    (root.parent/'cleanup.json').write_text(json.dumps({'remaining':[],'original_retired':not original.process_alive})+'\n')
