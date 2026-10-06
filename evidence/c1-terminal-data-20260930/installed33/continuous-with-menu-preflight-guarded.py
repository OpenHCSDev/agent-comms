import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,'/home/ts/wt/toad-native-fixture-cleanup-20260930/tests')
from input_visibility_native_installed_pilot import InstalledApp as OriginalApp,InputRaster,acceptance,review_frames
from l0a_native_installed_pilot import main,until
from saved_state_user_journey_pilot import reveal_thread_row
from toad.widgets.comms_sidebar import ChannelGroup
from toad.widgets.comms_menu import ContextMenu,ContextMenuItem
from toad.thread_actions import ForkAction


class InstalledApp(OriginalApp):
    """Observe the same emitted DTO; diagnostics never modify delivery or paint."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.measurement_terminal=None
        self.measurement_stream=None
        self.measurement_inputs={}

    def observe_input_paint(self,frame):
        super().observe_input_paint(frame)
        import pyte
        if self.measurement_terminal is None:
            self.measurement_terminal=InputRaster(frame.width,frame.height)
            self.measurement_stream=pyte.Stream(self.measurement_terminal)
        terminal=self.measurement_terminal
        terminal.paint_frame=frame
        terminal.resize(lines=frame.height,columns=frame.width)
        self.measurement_stream.feed(frame.ansi)
        for item in frame.submissions:
            self.measurement_inputs[item.request.input_id]=item.request.user_text
        for item in frame.queue:
            self.measurement_inputs[item.input_id]=item.text
        for item in frame.mounted_starts:
            self.measurement_inputs[item.input_id]=item.text
        visible='\n'.join((terminal.region_text(lambda f:f.queue_region),
                           terminal.region_text(lambda f:f.chat_region)))
        evidence=Path(os.environ['L0A_EVIDENCE'])
        for input_id,text in self.measurement_inputs.items():
            if text and visible.count(text)>1 and not (evidence/'duplicate-at-emission.json').exists():
                self.capture_duplicate(frame,input_id,text,evidence)
                return

    def capture_duplicate(self,frame,input_id,text,evidence):
        from agent_comms.field_codec import FieldCodec
        from agent_comms.comms import wire
        from agent_comms.private_path import FileRevision
        from agent_comms.input_disposition import InputDispositions
        from toad.widgets.transcript_history import TranscriptHistory
        from toad.widgets.committed_presentation import CommitParticipant
        view=self.selected_session.conversation
        source={'phase':'first duplicated emitted incremental ANSI before asynchronous yield/teardown',
            'original_frame':FieldCodec.encode(frame),'input_id':input_id,'text':text,
            'contents':[{'resource_id':id(w),'type':type(w).__name__,'mounted':w.is_mounted,
                'attached':w.is_attached,'region':list(w.region),
                'commit_claim':repr(w.commit_claim)} for w in view.contents.children
                if isinstance(w,CommitParticipant)],
            'histories':[{'resource_id':id(h),'state':type(h.state).__name__,
                'through':FieldCodec.encode(h.through),
                'pages':[FieldCodec.encode(page.page) for page in h.pages]}
                for h in view.contents.query(TranscriptHistory)],
            'history_lock_held':view.window.history_lock.locked(),
            'workers':[{'name':w.name,'state':str(w.state),'error':repr(w.error)} for w in self.workers]}
        # Persist the original frame/resource graph before any filesystem read.
        (evidence/'duplicate-at-emission.json').write_text(json.dumps(source,indent=2)+'\n')
        (evidence/'duplicate-at-emission-terminal.txt').write_text('\n'.join(self.measurement_terminal.display)+'\n')
        with (evidence/'duplicate-at-emission-tasks.txt').open('w') as out:
            for task in asyncio.all_tasks():
                print(repr(task),file=out);task.print_stack(file=out)
        try:
            comms=wire()
            fixture=Path(os.environ['INPUT_VISIBILITY_FIXTURE']).resolve()
            assert comms.root.resolve().is_relative_to(fixture)
            with comms.registry.store.locked(shared=True,blocking=False):
                thread=comms.registry.require(frame.session_id)
                source['original_registry_thread']=FieldCodec.encode(thread)
            rows=InputDispositions(comms.root/InputDispositions.filename)
            with rows.locked(shared=True,blocking=False):
                source['original_input_disposition']=FieldCodec.encode(rows.read().lookup('acp:'+input_id))
            if thread.session_file is not None:
                path=Path(thread.session_file);assert path.resolve().is_relative_to(fixture)
                with path.open('rb') as native:
                    import os
                    before=FileRevision.from_stat(os.fstat(native.fileno()))
                    assert before.size<=8*1024*1024,'Retained controlled source exceeds diagnostic bound'
                    payload=native.read()
                    after=FileRevision.from_stat(os.fstat(native.fileno()))
                (evidence/'duplicate-at-emission-native.jsonl').write_bytes(payload)
                source['native_file_observation']={'path':str(path),'before':FieldCodec.encode(before),
                    'after':FieldCodec.encode(after),'unchanged_during_read':before==after}
        except BaseException as error:
            source['original_source_capture_error']=repr(error)
        (evidence/'duplicate-at-emission.json').write_text(json.dumps(source,indent=2)+'\n')

async def run_continuous(app,pilot,agent,comms,entered,release,hold_next,requests):
 evidence=Path(os.environ['L0A_EVIDENCE']);original_screen=app.screen
 row=await reveal_thread_row(app,pilot,'beta','#team')
 group=row.query_ancestor(ChannelGroup);point=row.region.offset;hit,_=app.screen.get_widget_at(*point)
 receipt={'target':row.target_name,'channel':group.row.target_name,'row_region':list(row.region),'screen_point':list(point),'native_hit_is_original_row':hit is row,'native_hit_type':type(hit).__name__,'group_expanded':group.expanded,'provider_posts_before_seed':len(requests),'native_inputs_sent':0,'same_installed_app':True,'physical_driver':type(app._driver).__name__,'headless':app.is_headless}
 (evidence/'preseed-menu-preflight.json').write_text(json.dumps(receipt,indent=2)+'\n')
 assert await pilot.click(row,button=3)
 await until(pilot,lambda:isinstance(app.screen,ContextMenu) and bool(app.screen.query(ContextMenuItem)))
 assert any(item.action==ForkAction.declared_name for item in app.screen.query(ContextMenuItem))
 app.save_screenshot(str(evidence/'preseed-menu.svg'))
 receipt.update(menu_opened=True,pointer_after_click=list(app.mouse_position))
 (evidence/'preseed-menu-preflight.json').write_text(json.dumps(receipt,indent=2)+'\n')
 await pilot.press('escape')
 await until(pilot,lambda:app.screen is original_screen)
 assert not requests
 receipt.update(declared_menu_escape_closed=True,provider_posts_before_seed=len(requests))
 (evidence/'preseed-menu-preflight.json').write_text(json.dumps(receipt,indent=2)+'\n')
 await acceptance(app,pilot,agent,comms,entered,release,hold_next,requests)

async def continuous(app,pilot,agent,comms,entered,release,hold_next,requests):
    try:
        await run_continuous(app,pilot,agent,comms,entered,release,hold_next,requests)
    except BaseException as original:
        import time,traceback
        from agent_comms.field_codec import FieldCodec
        evidence=Path(os.environ['L0A_EVIDENCE'])
        (evidence/'original-before-teardown.txt').write_text(traceback.format_exc())
        source={'observed_ns':time.monotonic_ns(),'original_error_type':type(original).__name__,
                'original_error':str(original),'screen_type':type(app.screen).__name__,
                'provider_posts':len(requests),'fixture_root':str(comms.root),
                'native_driver':type(app._driver).__name__,'headless':app.is_headless,
                'capture_phase':'after_original_predicate_failure_before_fixture_teardown'}
        with (evidence/'original-tasks-before-teardown.txt').open('w') as tasks_output:
            for task in asyncio.all_tasks():
                print(repr(task),file=tasks_output)
                task.print_stack(file=tasks_output)
        try:
            source['screen_pump']={'mounted':app.screen.is_mounted,
                'attached':app.screen.is_attached,'running':app.screen.is_running,
                'message_queue_size':app.screen.message_queue_size,
                'task':repr(app.screen._task)}
            source['original_workers']=[{'name':worker.name,'group':worker.group,
                'state':str(worker.state),'error':repr(worker.error)} for worker in app.workers]
            source['canonical_threads']={name:FieldCodec.encode(thread) for name,thread in comms.registry.all_threads().items()}
            source['native_dom']=[{'type':type(widget).__name__,'id':widget.id,
                'region':list(widget.region),'mounted':widget.is_mounted,'attached':widget.is_attached,
                'display':widget.display} for widget in app.screen.walk_children()]
            source['focused_type']=type(app.focused).__name__
            source['focused_id']=app.focused.id if app.focused is not None else None
            app.save_screenshot(str(evidence/'failure-before-teardown.svg'))
        except BaseException as capture_error:
            source['capture_error']=repr(capture_error)
        (evidence/'original-state-before-teardown.json').write_text(json.dumps(source,indent=2)+'\n')
        raise

if __name__ == '__main__':
    asyncio.run(main(app_type=InstalledApp,acceptance=continuous,expected_response_disconnects=frozenset({3,5}),provider_request_budget=5,headless=False,fixture_stage=Path(os.environ['INPUT_VISIBILITY_FIXTURE'])))
    evidence=Path(os.environ['L0A_EVIDENCE'])
    (evidence/'input-visibility-review.json').write_text(json.dumps(review_frames(evidence/'input-frames.jsonl'),indent=2)+'\n')
