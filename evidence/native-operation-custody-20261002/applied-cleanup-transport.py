import hashlib, inspect, json, time
from dataclasses import replace
from pathlib import Path
from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.registration import Registration
from agent_comms.store_files import _store_lock
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import ThreadIncarnation

root=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
out=Path('/home/ts/.cache/agent-scratch/mendel-native-operation547-20261002')
name='dedicated-worktree-cleanup-20261001'
expected=ThreadIncarnation(name,1790874801.3895755)
process=ProcessIdentity(702428,44958513)
registration=Registration(root/'registry.json')
started=time.monotonic()
with _store_lock(root/'wire'), registration.store.editing() as edit:
    snapshot=edit.document.snapshot()
    original=snapshot.require_active(name)
    if original.incarnation!=expected or original.require_process()!=process:
        raise RuntimeError('Original cleanup incarnation/process changed; no correction')
    Platform.current().require(process)
    original.require_idle()
    if original.task!='Sole authorized disk cleanup owner; bounded retained-worktree internals audit; preserve original sessions, sources, frozen/native build donors and current prefixes. No provider process or goal.':
        raise RuntimeError('Original explicit cleanup task changed; no correction')
    if original.goal is not None or original.session_file is not None:
        raise RuntimeError('Cleanup acquired goal/native source; no correction')
    captured=OwnerRestartSelection.capture(snapshot,name)
    captured.require_current(edit.document.snapshot())
    inputs=InputDispositions(root/InputDispositions.filename)
    before_inputs={k:v for k,v in inputs.read().rows.items() if v.matches_owner(expected)}
    original_record=FieldCodec.encode(original)
    (out/'cleanup-original-thread.json').write_text(json.dumps(original_record,indent=2)+'\n')
    old_document=edit.document.copy()
    installed=registration._declare_in(edit,replace(original,execution=ExternalThreadExecution),snapshot.statuses[name])
    if replace(installed,execution=original.execution)!=original:
        raise RuntimeError('Registry change altered other original Thread fields')
    after=edit.document.snapshot()
    if {k:v for k,v in after.threads.items() if k!=name}!={k:v for k,v in snapshot.threads.items() if k!=name}:
        raise RuntimeError('Registry operation altered unrelated Thread declarations')
    if after.statuses!=snapshot.statuses or after.aliases!=snapshot.aliases:
        raise RuntimeError('Registry operation altered status/aliases')
    if {k:v for k,v in inputs.read().rows.items() if v.matches_owner(expected)}!=before_inputs:
        raise RuntimeError('Original cleanup inputs changed')
    Platform.current().require(process)
    receipt={'result':'PASS','owner':'Mendel','operation':'Existing installed Registration._declare_in via RegistryStore.editing','api_source':inspect.getfile(Registration),'root':str(root),'thread':name,'incarnation':FieldCodec.encode(expected),'process':FieldCodec.encode(process),'idle_verified':True,'original_task_no_provider_or_goal':True,'before_execution':original.execution.declared_name,'after_execution':installed.execution.declared_name,'whole_thread_equal_except_execution':True,'other_threads_status_aliases_equal':True,'original_inputs_unchanged':True,'original_input_count':len(before_inputs),'before_owner_generation':snapshot.owner_generations[name],'after_owner_generation':after.owner_generations[name],'before_admission':snapshot.admission_generations[name],'after_admission':after.admission_generations[name],'process_alive_after':True,'signal':False,'provider_calls':0,'new_input':False,'replay':False,'default_changed':False,'elapsed_seconds':time.monotonic()-started,'original_thread_preimage':str(out/'cleanup-original-thread.json'),'original_thread_preimage_sha256':hashlib.sha256((out/'cleanup-original-thread.json').read_bytes()).hexdigest()}
(out/'cleanup-transport-correction.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
