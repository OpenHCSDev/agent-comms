"""Real Toad, private wire, plain-st profile; no provider, test runner or shared state."""
import asyncio, importlib.util, json, os, sys, threading
from pathlib import Path

stage = Path(sys.argv[1]); mode = sys.argv[2]
stage.mkdir(parents=True, exist_ok=True)
os.environ.update(AGENT_COMMS_ROOT=str(stage/'wire'), XDG_CONFIG_HOME=str(stage/'config'),
                  XDG_STATE_HOME=str(stage/'state'), XDG_DATA_HOME=str(stage/'data'))
from agent_comms.launch_runtime import ROOT_ID_ENV, PACKAGE_ENV
os.environ.pop(ROOT_ID_ENV, None); os.environ.pop(PACKAGE_ENV, None)
from agent_comms.comms import Comms
from agent_comms.threads import Thread
service = Comms(stage/'wire')
root_id = service.messaging.initialize_private_initial_protocol()
service.registry.declare(Thread('owner', frozenset({'team'}), str(stage)))
for i in range(8):
    name=f'peer-{i}'
    service.registry.declare(Thread(name, frozenset({'team'}), str(stage), parent='owner'))
    service.messaging.send_message(name, 'owner', f'Original message {i}')

if mode == 'before':
    name='toad.widgets.thread_comms'
    spec=importlib.util.spec_from_file_location(name, Path(__file__).with_name('before_thread_comms.py'))
    module=importlib.util.module_from_spec(spec); sys.modules[name]=module; spec.loader.exec_module(module)
from toad.widgets.thread_comms import ThreadCommsSidebar
from agent_comms.ui_model.sidebar import ThreadRowModel
from textual.content import Content
counters={'rows':0,'cells':0,'measurement_threads':set()}
original_row = ThreadRowModel.of.__func__
def measured_row(cls, person, snapshot):
    if 'thread_comms' in sys._getframe(1).f_code.co_filename:
        counters['rows'] += 1
    return original_row(cls, person, snapshot)
ThreadRowModel.of=classmethod(measured_row)
original_cell=Content.cell_length.fget
def measured_cell(self):
    if 'thread_comms' in sys._getframe(1).f_code.co_filename:
        counters['cells'] += 1; counters['measurement_threads'].add(threading.get_ident())
    return original_cell(self)
Content.cell_length=property(measured_cell)

from toad.app import ToadApp
from toad.application_lifetime import ApplicationLifetime
from toad.session_entry import HistorySessionEntry
from toad.session_tracker import CommsViewKey
from toad.conversation_kind import DmConversation
from toad.widgets.side_bar import SideBar
app=ToadApp(project_dir=str(stage))

async def until(predicate, seconds=20):
    async with asyncio.timeout(seconds):
        while not predicate():
            if app._exception is not None: raise app._exception
            await asyncio.sleep(.05)

async def capture(name):
    process=await asyncio.create_subprocess_exec('import','-window',os.environ['WINDOWID'],str(stage/f'{name}.png'))
    assert await process.wait()==0

async def journey():
    try:
        await until(lambda: bool(app.selected_mode))
        key=CommsViewKey(str(service.root), app.selected_mode, 'owner', DmConversation, 'peer-7')
        entry=HistorySessionEntry('p78-dm', key, DmConversation, stage, str(service.root), ())
        await app.session_navigation.open(entry)
        await until(lambda: app.screen.query_one_optional(ThreadCommsSidebar) is not None)
        panel=app.screen.query_one(ThreadCommsSidebar)
        sidebar=panel.query_ancestor(SideBar)
        if not sidebar.presentation_visible: sidebar.toggle(focus=False)
        await until(lambda: panel._snapshot is not None and len(panel.thread_rows)==8)
        await asyncio.sleep(.5)
        await capture('before-dm')
        previous=dict(panel.thread_rows); revision=panel._revision
        counters.update(rows=0,cells=0,measurement_threads=set())
        message=await app.preparation.run_thread(service.messaging.send_message,'peer-0','owner','ONE DISTINCT P78 DM')
        panel.refresh_relationships(force=True)
        await until(lambda: panel._revision != revision and panel._refresh_task.done())
        await asyncio.sleep(.2)
        await capture('after-dm')
        result={'mode':mode,'rows_built':counters['rows'],'cells_measured':counters['cells'],
                'reused_rows':sum(panel.thread_rows.get(name) is row for name,row in previous.items()),
                'measurement_threads':sorted(counters['measurement_threads']),
                'ui_thread':threading.get_ident(),'root_id':root_id,'message_reference':{'seq':message.seq,'id':message.message_id},
                'toad_source':str(Path(sys.modules['toad.widgets.thread_comms'].__file__)),
                'physical_terminal':sys.__stdout__.isatty()}
        assert result['physical_terminal']
        assert result['ui_thread'] not in result['measurement_threads']
        (stage/'result.json').write_text(json.dumps(result,indent=2))
    except BaseException:
        import traceback
        (stage/'failure.txt').write_text(traceback.format_exc())
        raise
    finally:
        app.exit()

ApplicationLifetime.observe_next_start(lambda lifetime: app.call_after_refresh(lambda: app.run_worker(journey(), exit_on_error=True)))
app.run()
