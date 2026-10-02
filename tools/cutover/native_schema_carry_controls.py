"""Final installed-operator controls; original producer fixtures, zero provider."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.field_codec import FieldCodec
from native_schema_carry import NativeSchemaDeclaration, NativeSchemaCarryPlan, prepare
from runtime_installation import CarryNativeRuntimeInstallation, RuntimeInstallation
from retained_summary_reset import RuntimeCompactionFiles
from publish_openhcs_recovery import digest


def seed(base, *, full_release=False):
    base=base.absolute()
    if os.environ.get('SCHEMA_CARRY_FIXTURE_DEPENDENCIES'):
        sys.path.append(os.environ['SCHEMA_CARRY_FIXTURE_DEPENDENCIES'])
    sys.path.extend(filter(None, os.environ.get('SCHEMA_CARRY_FIXTURE_EXTRA_DEPENDENCIES', '').split(os.pathsep)))
    sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'tests'))
    from test_native_source_cursor_certificate import _fresh, _seal
    from agent_comms.coordinator import Coordination
    from agent_comms.selected_participant import SelectedParticipant
    from agent_comms.private_send_stage import TriageNativeSend
    from agent_comms.native_input_owner import ParticipantOwner
    from agent_comms.native_prompt_binding import bind_expected_prompt
    from agent_comms.native_runtime_input import NativeRuntimeInput, CurrentNativeCursor
    from agent_comms.native_pi import NativeContextProof
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.compaction_journal import CompactionJournal
    from native_proof_cases import write_proof_rows
    base.mkdir(mode=0o700)
    root, root_id, comms = _fresh(base, 2)
    triage_message = _seal(comms,root,root_id,'#team','original uncertain triage')
    with Coordination(str(root/'coordination.sqlite3')) as store:
        with SelectedParticipant.select(comms,store,root_id,'alpha',0) as participant:
            stage = TriageNativeSend(participant.assignment)
            owner = ParticipantOwner(participant.owner.thread,participant.identity.generation)
            triage_id = stage.reserve(store,owner,'a'*64)
            bind_expected_prompt(store,input_id=triage_id,stage=stage,owner=owner.thread,generation=owner.generation,prompt='original triage prompt')
            inputs=InputDispositions(root / InputDispositions.filename)
            key=inputs.bus_key(triage_message,owner.thread)
            inputs.record(key,seq=triage_message.seq,owner=owner.thread.name,admission=participant.owner.admission_generation,target=triage_message.target,text=triage_message.body)
            inputs.bind(key,admission=participant.owner.admission_generation,turn_id=participant.owner.thread.active_turn.id,native_id=triage_id,text='original triage prompt')
    full_message = _seal(comms,root,root_id,'#team','@other001 original full proof fixture')
    with Coordination(str(root/'coordination.sqlite3')) as store:
        with SelectedParticipant.select(comms,store,root_id,'other001',0) as participant:
            from agent_comms.coordination_tables.executions import ExecutionOrigin
            from agent_comms.attempt_start import AttemptStart
            from agent_comms.durable_turn import DurableTurn
            from agent_comms.private_send_stage import FullNativeSend
            from agent_comms.coordination_tables.assignments import WakeAssignment
            from agent_comms.owner_fence import prepare_fence_token
            assignments=WakeAssignment.select(store.session._connection,where='recipient_lookup=?',parameters=(participant.lookup,),order_by=('wire_seq',))
            ids=tuple(row.assignment_id for row in assignments)
            assert len(ids)==2
            execution_id='wirev1'+hashlib.sha256(repr(ids).encode()).hexdigest()
            store.executions.create(execution_id,ExecutionOrigin.WIRE,participant.lookup,'other001',1,assignment_ids=ids,exact_target='#team')
            snapshot=store.snapshots.get(execution_id)
            snapshot=store.executions.mark_pending(execution_id,expected_revision=snapshot.execution.revision).value
            started=store.attempts.start(AttemptStart(execution_id,1,'other001',participant.identity.generation,prepare_fence_token(),expected_execution_revision=snapshot.execution.revision,expected_pointer_revision=snapshot.pointer_revision)).value
            stage=FullNativeSend(started.snapshot.assignments[0],DurableTurn(store.attempts,started.fence,started.snapshot.pointer_revision,''))
            owner=ParticipantOwner(participant.owner.thread,participant.identity.generation)
            input_id=stage.reserve(store,owner,'b'*64)
            prompt='original full fixture prompt'
            prompt_digest=bind_expected_prompt(store,input_id=input_id,stage=stage,owner=owner.thread,generation=owner.generation,prompt=prompt)
            directory=root/'native-sessions'/participant.lookup
            directory.mkdir(mode=0o700,parents=True)
            session=directory/'original.jsonl'
            session.write_text(json.dumps({'type':'session','id':'sid','version':3})+'\n'+json.dumps({'type':'message','id':'entry','message':{'role':'user','inputId':input_id,'inputDigest':prompt_digest,'content':[{'type':'text','text':prompt}]}})+'\n')
            session.chmod(0o600)
            write_proof_rows(session,[{'schema':1,'type':'context_committed','sessionId':'sid','inputId':input_id,'sessionEntryId':'entry','requestGeneration':1,'llmContextDigest':'d'*64}])
            context=NativeContextProof(input_id,'sid','entry',1,'d'*64,session)
            with store.session.transaction() as db:
                row=NativeRuntimeInput.one(db,input_id=input_id)
                row.sent_owner_admission_generation.record(row,db,participant.owner.admission_generation)
                row.commit_context(db,context)
                CurrentNativeCursor(wire_root_id=root_id,recipient_lookup=participant.lookup,owner_thread=owner.thread.name,owner_generation=owner.generation,owner_admission_generation=participant.owner.admission_generation,covered_seq=stage.assignment.wire_seq,injected_seq=stage.assignment.wire_seq,input_id=input_id,assignment_id=stage.assignment.assignment_id,stage=type(stage.execution),session_id='sid',request_generation=1).insert(db)
            if full_release:
                from agent_comms.attempt_states import PromptAcceptedAttempt, ModelRunningAttempt, SettlingAttempt
                from agent_comms.coordination_response import LiveResponseOwner, prepare_fenced_response, publish_fenced_response
                from agent_comms.message_bus import MessageBus
                fence = started.fence
                for state in (PromptAcceptedAttempt, ModelRunningAttempt, SettlingAttempt):
                    facts = {'backend_done': True, 'process_dead': True} if state is SettlingAttempt else {}
                    fence = store.attempts.advance(fence, state,
                        expected_pointer_revision=started.snapshot.pointer_revision, **facts).value.fence
                response_bus = MessageBus(root/'bus.jsonl', comms.registry, private_response_writes=True)
                witness = LiveResponseOwner(thread=participant.owner.thread,
                                            admission_generation=participant.owner.admission_generation)
                prepare_fenced_response(store, response_bus, fence, 'Original frozen response',
                                        timestamp=123.5, owner_witness=witness)
                publish_fenced_response(store, response_bus, fence, owner_witness=witness)
    CompactionJournal(root/'compaction-commits.sqlite3')
    return {'source_package':__import__('agent_comms').__file__,'root':str(root),'triage_id':triage_id,'full_id':input_id,'seed_strength':'authentic installed4 producers and representative typed proof fixture; no native execution/provider call'}


def describe_original(root):
    from agent_comms.coordinator import Coordination
    from agent_comms.native_runtime_input import NativeRuntimeInput
    with Coordination(str(root/'coordination.sqlite3')) as store:
        originals = NativeRuntimeInput.select(store.session._connection)
    return {'source_package': __import__('agent_comms').__file__, 'root': str(root),
            'triage_id': next(row.input_id for row in originals if row.execution.binding_fields()['execution_id'] is None),
            'full_id': next(row.input_id for row in originals if row.execution.binding_fields()['execution_id'] is not None),
            'seed_strength': 'reused preserved authentic original8/2/2/4 fixture; no producer replay'}


def run(base,source_python, *, full_release=False, resume_original=None):
    base=base.absolute()
    base.mkdir(mode=0o700)
    environment=dict(os.environ)
    environment.pop('PYTHONPATH',None)
    import sysconfig
    environment['SCHEMA_CARRY_FIXTURE_DEPENDENCIES']=sysconfig.get_path('purelib')
    import site
    environment['SCHEMA_CARRY_FIXTURE_EXTRA_DEPENDENCIES']=os.pathsep.join(site.getsitepackages())
    args = [str(source_python), str(Path(__file__).resolve()),
            '--describe-original' if resume_original else '--seed',
            str(resume_original if resume_original else base/'original')]
    if full_release:
        args.append('--full-release')
    packet=subprocess.run(args,env=environment,text=True,capture_output=True)
    if packet.returncode:
        raise RuntimeError(packet.stderr)
    seeded=json.loads(packet.stdout)
    (base/'seed-provenance.json').write_text(json.dumps(seeded,indent=2)+'\n')
    root=Path(seeded['root'])
    original_packet=subprocess.run([str(source_python),str(Path(__file__).with_name('native_schema_carry.py').resolve()),'--declaration'],text=True,capture_output=True,check=True)
    original=FieldCodec.decode(NativeSchemaDeclaration,json.loads(original_packet.stdout))
    protected={str(path):digest(path) for path in root.rglob('*') if path.is_file() and path.name not in ('coordination.sqlite3','native_prompt_bindings.sqlite3')}
    source_hashes={name:digest(root/name) for name in ('coordination.sqlite3','native_prompt_bindings.sqlite3')}
    plan=prepare(root,base/'matched-candidate',original)
    assert source_hashes=={name:digest(root/name) for name in source_hashes}
    installed=CarryNativeRuntimeInstallation(goal_schema=plan.original.goal, plan=plan)
    assert FieldCodec.decode(RuntimeInstallation,FieldCodec.encode(installed))==installed
    original_package=seeded.get('source_package')
    if original_package is None or not Path(original_package).is_relative_to(source_python.absolute().parent.parent):
        raise AssertionError('Fixture did not use the authentic installed original package')
    refusals=[]
    for case in (() if full_release else ('candidate-change','source-change','existing-attempt','companion','unknown-snapshot')):
        try:
            if case=='existing-attempt':
                attempt_dir=base/'preexisting-attempt';attempt_dir.mkdir(mode=0o700)
                plan.install(attempt_dir)
            elif case=='candidate-change':
                path=plan.candidate/'coordination.sqlite3'
                prior=path.read_bytes();path.write_bytes(prior+b'changed')
                try: plan.require_candidate()
                finally: path.write_bytes(prior)
            elif case=='source-change':
                path=root/'coordination.sqlite3'
                prior=path.read_bytes();path.write_bytes(prior+b'changed')
                try: plan.install(base/'must-not-install')
                finally: path.write_bytes(prior)
            elif case=='companion':
                from native_schema_carry import RuntimeNativeFiles
                path=root/'coordination.sqlite3-wal';path.write_bytes(b'owned control companion');path.chmod(0o600)
                try: RuntimeNativeFiles(root).acquire()
                finally: path.unlink()
            else:
                from native_schema_carry import RuntimeNativeFiles
                path=root/'.native_prompt_bindings.sqlite3.pending';path.write_bytes(b'owned control uncertainty');path.chmod(0o600)
                try: RuntimeNativeFiles(root).acquire()
                finally: path.unlink()
        except (ValueError,RuntimeError,FileExistsError):
            refusals.append(case)
        else:
            raise AssertionError('Refusal missing: '+case)
        assert source_hashes=={name:digest(root/name) for name in source_hashes}
    originals=base/'originals'
    originals.mkdir(mode=0o700)
    with RuntimeCompactionFiles(root).acquire() as acquired:
        receipt=installed.install(acquired,originals/'native')
    assert all(digest(originals/'native'/name)==sha for name,sha in source_hashes.items())
    assert protected=={path:digest(Path(path)) for path in protected}
    from agent_comms.coordinator import Coordination
    from agent_comms.coordinated_runtime_schema import assert_native_runtime_schema
    from agent_comms.native_runtime_input import NativeRuntimeInput, CurrentNativeCursor
    from agent_comms.native_prompt_binding import read_expected_prompt_binding
    with Coordination(str(root/'coordination.sqlite3')) as store:
        assert_native_runtime_schema(store.session._connection)
        inputs=NativeRuntimeInput.select(store.session._connection)
        assert {row.input_id for row in inputs}=={seeded['triage_id'],seeded['full_id']}
        triage=NativeRuntimeInput.one(store.session._connection,input_id=seeded['triage_id'])
        assert triage.session_id is None
        assert len(triage.execution.source_assignment_ids(store.session._connection,triage.input_id))==1
        full=NativeRuntimeInput.one(store.session._connection,input_id=seeded['full_id'])
        assert full.llm_context_digest=='d'*64
        assert len(full.execution.source_assignment_ids(store.session._connection,full.input_id))==2
        assert CurrentNativeCursor.select(store.session._connection)[0].input_id==full.input_id
        assert read_expected_prompt_binding(store,full.input_id).expected_prompt_digest
        if full_release:
            from agent_comms.coordination_tables.publications import PublicationIntents, PublicationReceipts
            from agent_comms.coordination_response import PublicationAppendDispatches
            from agent_comms.coordination_tables.responses import ResponseObligation
            db = store.session._connection
            obligations = ResponseObligation.select(db)
            intents = PublicationIntents.select(db)
            receipts = PublicationReceipts.select(db)
            dispatches = PublicationAppendDispatches.select(db)
            assert len(obligations) == len(intents) == len(receipts) == len(dispatches) == 1
            assert obligations[0].exact_target == intents[0].exact_target == receipts[0].exact_target == dispatches[0].exact_target == '#team'
            snapshot = store.snapshots.get(full.execution.execution_id)
            assert snapshot.snapshot_version == 3 and snapshot.completed_response()
            assert len(snapshot.assignments) == 2 and len(snapshot.obligations) == 1
            from agent_comms.comms import Comms
            reopened = Comms(root, private_initial_writes=False, private_claim_writes=False)
            response = reopened.bus.log.read_keyed_response(intents[0])
            assert response is not None and response.message_id == receipts[0].message_id
            assert response.body == 'Original frozen response'
    result={'state':'installed-operator-carry-controls-passed','seed':seeded,'source_python':str(source_python),'target_python':sys.executable,'target_package':__import__('agent_comms').__file__,'installation':receipt,'unchanged_protected_files':len(protected),'refusals':refusals,'public_mutations':0,'owner_stops':0,'provider_calls':0,'native_inputs':0,'strength':'installed4 producer fixture -> matched candidate -> existing RuntimeInstallation member -> installed5 schema/binding/cursor readers; not public activation or whole stopped-owner publication'}
    if full_release:
        result.update(state='installed-complete490-carry-journey-passed',
                      original_release=plan.original.release_versions,
                      target_release=plan.target.release_versions,
                      strength='installed original8/2/2/4 producer and durable response -> one matched clone -> actual RuntimeInstallation member -> installed9/3/3/5 coordinator snapshot/response/native/binding/cursor and original keyed-bus readers; not public cohort activation',
                      historical_controls_rerun=False, original_obligations=1,
                      original_intents=1, original_receipts=1, original_append_dispatches=1)
    (base/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    return {'state':result['state'],'receipt':str(base/'receipt.json'),'refusals':refusals}


def goal_journey(base, original_db, declaration):
    """Actual original ledger -> preserving member -> strict target store."""
    from contextlib import closing
    import sqlite3
    from native_schema_carry import inventory, row_digest, rows
    from retained_summary_reset import RuntimeGoalFiles
    from runtime_installation import PreserveRuntimeInstallation
    from publish_openhcs_recovery import retain_file
    from agent_comms.goal_attempts import GoalAttemptSchema, GoalAttemptStore, StorageUncertainError
    base = base.absolute()
    base.mkdir(mode=0o700)
    root = base / 'root'
    root.mkdir(mode=0o700)
    goal_root = root / 'goal-private'
    goal_root.mkdir(mode=0o700)
    path = goal_root / 'goal_attempts.sqlite3'
    retain_file(original_db, path)
    original_sha = digest(path)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        before = inventory(db)
    try:
        GoalAttemptStore(goal_root)
    except StorageUncertainError:
        pass
    else:
        raise AssertionError('Original incompatible ledger unexpectedly admitted')
    source_schema = json.loads(declaration.read_text())
    installation = PreserveRuntimeInstallation(goal_schema=source_schema)
    assert FieldCodec.decode(RuntimeInstallation, FieldCodec.encode(installation)) == installation
    # Wrong declaration must not turn into a guessed migration.
    with RuntimeGoalFiles(root).acquire() as acquired:
        try:
            PreserveRuntimeInstallation(goal_schema={}).synchronize_goal(acquired, base/'wrong-preimage')
        except ValueError:
            pass
        else:
            raise AssertionError('Unknown goal declaration was transformed')
        assert digest(path) == original_sha
        receipt = installation.synchronize_goal(acquired, base/'originals')
    assert digest(base/'originals/goal-private/goal_attempts.sqlite3') == original_sha
    store = GoalAttemptStore(goal_root)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        after = inventory(db)
        marker = GoalAttemptSchema.select(db)
    facts = before.keys() - {GoalAttemptSchema.declared_name}
    assert all(before[name] == after[name] for name in facts)
    assert marker == [GoalAttemptSchema.current()]
    for goal_id, *_ in before['generation']:
        assert store.snapshot(goal_id) is not None
    with RuntimeGoalFiles(root).acquire() as acquired:
        matched = installation.synchronize_goal(acquired, base/'must-not-recarry')
        acquired.require_original()
    assert not (base/'must-not-recarry').exists()
    result = {'state': 'original-goal-ledger-carry-passed', 'target_python': sys.executable,
              'target_package': __import__('agent_comms').__file__,
              'original_db': str(original_db), 'original_copy_sha256': original_sha,
              'source_declaration_sha256': digest(declaration), 'installation': receipt,
              'unchanged_fact_rows': sum(len(before[name]) for name in facts),
              'unchanged_fact_rows_sha256': row_digest({name: before[name] for name in sorted(facts)}),
              'matched_declaration_preserved': matched,
              'refusals': ['original-strict-schema', 'unauthenticated-declaration'],
              'native_inputs': 0, 'provider_calls': 0, 'public_mutations': 0, 'owner_stops': 0,
              'strength': 'copied running-source inventory; actual RuntimeInstallation and strict GoalAttemptStore; not stopped public carry or native/UI journey'}
    (base/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
    return {'state': result['state'], 'receipt': str(base/'receipt.json')}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',type=Path)
    parser.add_argument('--base',type=Path)
    parser.add_argument('--source-python',type=Path)
    parser.add_argument('--full-release',action='store_true')
    parser.add_argument('--describe-original',type=Path)
    parser.add_argument('--resume-original',type=Path)
    parser.add_argument('--goal-original-db', type=Path)
    parser.add_argument('--goal-original-declaration', type=Path)
    args=parser.parse_args()
    if args.goal_original_db:
        if not args.base or not args.goal_original_declaration:
            parser.error('Goal carry requires fresh base and authentic original declaration')
        print(json.dumps(goal_journey(args.base, args.goal_original_db, args.goal_original_declaration)))
    elif args.describe_original:
        print(json.dumps(describe_original(args.describe_original)))
    else:
        print(json.dumps(seed(args.seed, full_release=args.full_release) if args.seed else run(args.base,args.source_python,full_release=args.full_release,resume_original=args.resume_original)))
