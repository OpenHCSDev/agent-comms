"""One installed selected workflow, original custody and controlled provider only."""
import asyncio, functools, hashlib, inspect, json, os, shutil, sqlite3, sys, tempfile, time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'tests'))
from compaction_loopback import LoopbackProvider
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordinator import Coordination
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.selected_turn import SelectedPrompt, SelectedAttempt, SelectedConsideration
from agent_comms.selected_request import SelectedRequest
from agent_comms.selected_session import SelectedSession
from agent_comms.private_send_admission import PrivateSendAdmission
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_startup import NativeStartupAdmission
from agent_comms.native_custody import PiSessionChild
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.registration import Registration
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.optional_awareness_projection import OptionalAwarenessProjection
import agent_comms.native_package as native_package
import agent_comms.private_send_admission as send_module
import agent_comms.selected_session as session_module

PACKAGE = Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
SOURCE = Path(os.environ['NATIVE_EVIDENCE_SOURCE'])
OUTPUT = Path(os.environ['NATIVE_EVIDENCE_RECEIPT'])
spans=[]


def instrument(resources, target, name, *, scope=False):
    descriptor=inspect.getattr_static(target,name)
    method=descriptor.__func__ if isinstance(descriptor,(classmethod,staticmethod)) else descriptor
    label=(target.__name__ if hasattr(target,'__name__') else type(target).__name__)+'.'+name
    def record(begin, end, phase='call'):
        spans.append({'operation':label,'phase':phase,'begin_ns':begin,'end_ns':end,'duration_ms':(end-begin)/1e6})
    if scope:
        @contextmanager
        @functools.wraps(method)
        def wrapped(*args,**kwargs):
            begin=time.perf_counter_ns()
            with method(*args,**kwargs) as value:
                entered=time.perf_counter_ns();record(begin,entered,'acquire')
                try:yield value
                finally:record(entered,time.perf_counter_ns(),'held')
    elif inspect.iscoroutinefunction(method):
        @functools.wraps(method)
        async def wrapped(*args,**kwargs):
            begin=time.perf_counter_ns()
            try:return await method(*args,**kwargs)
            finally:record(begin,time.perf_counter_ns())
    else:
        @functools.wraps(method)
        def wrapped(*args,**kwargs):
            begin=time.perf_counter_ns()
            try:return method(*args,**kwargs)
            finally:record(begin,time.perf_counter_ns())
    if isinstance(descriptor,classmethod):wrapped=classmethod(wrapped)
    elif isinstance(descriptor,staticmethod):wrapped=staticmethod(wrapped)
    resources.enter_context(patch.object(target,name,wrapped))


class Provider(LoopbackProvider):
    def response_chunks(self):
        if self.posts==1:
            yield {'content':'{"decision":"FULL"}'},None
            yield {},'stop'
        else:yield from super().response_chunks()


async def main():
    started=time.perf_counter_ns();base=Path(tempfile.mkdtemp(prefix='arendt-cause-489-fixed-',dir='/var/tmp'))
    base.chmod(0o700);root=base/'wire';root.mkdir(mode=0o700)
    source_hash=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    report={'installed_python':sys.executable,'core_module':native_package.__file__,
            'source_sha256':source_hash,'source_bytes':SOURCE.stat().st_size,'private_root':str(root),
            'spans':spans,'paid_calls':0,'public_inputs':0,'complete':False,
            'clock':'One Python process perf_counter_ns. Nested/overlapping spans are not additive.'}
    provider=Provider(status=200,text='PRIVATE_CAUSE_GREETING_OK')
    server=await asyncio.start_server(provider.handle,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1];config=base/'config';config.mkdir(mode=0o700)
    (config/'models.json').write_text(json.dumps({'providers':{'response-local':{
        'baseUrl':f'http://127.0.0.1:{port}/v1','api':'openai-completions',
        'models':[{'id':'fixture','name':'Controlled original custody','contextWindow':2000000,'maxTokens':256}]}}}))
    (config/'auth.json').write_text(json.dumps({'response-local':{'type':'api_key','key':'localhost-only'}}))
    (config/'settings.json').write_text(json.dumps({'compaction':{'enabled':False},'retry':{'enabled':False}}))
    os.environ.update(AGENT_COMMS_NATIVE_CONFIG_DIR=str(config),PI_CODING_AGENT_DIR=str(config),
                      AGENT_COMMS_ROOT=str(root),AC_NATIVE_COPIED_PACKAGE=str(PACKAGE))
    comms=Comms(root,private_initial_writes=True)
    identity=ProcessIdentity.capture(os.getpid())
    comms.registry.declare(Thread('sender',frozenset(),str(base),process_identity=identity))
    comms.registry.declare(Thread('beta',frozenset({'team'}),str(base),process_identity=identity,
                                 model='response-local/fixture',thinking_level='off'))
    rid=comms.messaging.initialize_private_initial_protocol()
    message=comms.messaging.send_initial_cohort('sender','#team','New isolated greeting: acknowledge once.')
    initial=comms.bus.log.read_delivery_cohort(rid,message.seq)
    with Coordination(str(root/'coordination.sqlite3')) as store:
        for audience in initial.audience.recipients:
            store.participants.register(audience.recipient_lookup,audience.canonical_thread,audience.canonical_thread,committed=True)
        accept_delivery_cohort(comms.bus,rid,message.seq,store)
    owner=comms.registry.require('beta');directory=root/'native-sessions'/stable_thread_lookup(owner.created_at)
    directory.mkdir(parents=True,mode=0o700);saved=directory/SOURCE.name;shutil.copyfile(SOURCE,saved);saved.chmod(0o600)
    with sqlite3.connect(Path(str(SOURCE)+'.input-proof').as_uri()+'?mode=ro',uri=True) as original:
        with sqlite3.connect(str(saved)+'.input-proof') as target:original.backup(target)
    Path(str(saved)+'.input-proof').chmod(0o600)
    comms.registry.register(replace(owner,session_file=str(saved)))
    try:
        with ExitStack() as resources:
            for target,names in (
                (SelectedPrompt,('full',)),(SelectedSession,('prepare',)),
                (SelectedAttempt,('engage','prepare','run')),(SelectedConsideration,('run',)),
                (SelectedRequest,('reserve',)),(NativePiRpcLaunch,('tracked',)),
                (NativeStartupAdmission,('acquire','release')),(PiSessionChild,('start','close')),
                (TrackedTurnSession,('attest','admit_prompt','committed_input','committed_context','context_proof','next_event')),
                (PrivateSendAdmission,('reserve','_saved_session','_admit_once','verify','commit')),
                (Registration,('transition_turn',)),
                (NativeSourceCursor,('advance',)),
                (SourceCoverage,('prefix','last_proof','evidence')), (OptionalAwarenessProjection,('for_selected','render')),
                (native_package,('verify_native_package',))):
                for name in names:instrument(resources,target,name)
            instrument(resources,send_module,'_response_boundary',scope=True)
            instrument(resources,session_module,'_response_boundary',scope=True)
            async with asyncio.timeout(90):
                result=await SelectedExecution(root=root,wire_root_id=rid,owner_name='beta',
                                              native_package=PACKAGE,session_file=saved).run()
            assert 'site-packages' in native_package.__file__
            with Coordination(str(root/'coordination.sqlite3')) as verified:
                (publication,) = result.publications
                receipt=verified.session._connection.execute(
                    'SELECT message_id,seq FROM publication_receipts WHERE message_id=?',
                    (publication.message_id,),
                ).fetchone()
            assert receipt is not None
            with comms.bus.log._record_snapshot() as (_,records):
                matches=[m for m,_ in records if m.reference.message_id==receipt[0] and m.seq==receipt[1]]
            assert len(matches)==1 and provider.text in matches[0].body
            assert result.cursor_status=='proven',result
            assert provider.posts==2,provider.posts
            assert comms.registry.require('beta').turn_lease is None
            report['complete']=True
    except BaseException as error:
        report['error']={'type':type(error).__name__,'detail':str(error)}
        raise
    finally:
        server.close();await server.wait_closed()
        report['duration_ms']=(time.perf_counter_ns()-started)/1e6
        report['provider_posts']=provider.posts
        report['original_unchanged']=hashlib.sha256(SOURCE.read_bytes()).hexdigest()==source_hash
        OUTPUT.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='spans'}))

asyncio.run(main())
