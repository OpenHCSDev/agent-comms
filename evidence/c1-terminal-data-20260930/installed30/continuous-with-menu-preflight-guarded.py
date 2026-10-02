import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,'/home/ts/wt/toad-input-delivery-visibility-20260930/tests')
from input_visibility_native_installed_pilot import InstalledApp,acceptance,review_frames
from l0a_native_installed_pilot import main,until
from saved_state_user_journey_pilot import reveal_thread_row
from toad.widgets.comms_sidebar import ChannelGroup
from toad.widgets.comms_menu import ContextMenu,ContextMenuItem
from toad.thread_actions import ForkAction

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
