import asyncio,json,os,sys,time
from pathlib import Path
sys.path.insert(0,'/home/ts/wt/toad-input-delivery-visibility-20260930/tests')
from input_visibility_native_installed_pilot import InstalledApp
from l0a_native_installed_pilot import main,until
from saved_state_user_journey_pilot import reveal_thread_row
from toad.widgets.comms_sidebar import ChannelGroup
from toad.widgets.comms_menu import ContextMenuItem
from toad.widgets.comms_fork_dialog import ForkDialog
from toad.thread_actions import ForkAction

async def menu_control(app,pilot,agent,comms,entered,release,hold_next,requests):
 evidence=Path(os.environ['L0A_EVIDENCE'])
 row=await reveal_thread_row(app,pilot,'beta','#team')
 group=row.query_ancestor(ChannelGroup);point=row.region.offset
 hit,_=app.screen.get_widget_at(*point)
 receipt={'driver':'3a3a36059da0c615990ff39f18754ab16ff064b1','target':row.target_name,'channel':group.row.target_name,'row_region':list(row.region),'screen_point':list(point),'native_hit_type':type(hit).__name__,'native_hit_is_original_row':hit is row,'expanded':group.expanded,'provider_posts':len(requests),'native_inputs_sent':0,'physical_driver':type(app._driver).__name__,'headless':app.is_headless}
 (evidence/'pointer-target.json').write_text(json.dumps(receipt,indent=2)+'\n')
 app.save_screenshot(str(evidence/'before-rightclick.svg'))
 assert await pilot.click(row,button=3)
 receipt.update(pointer_after_click=list(app.mouse_position),screen_after_click=type(app.screen).__name__)
 (evidence/'pointer-target.json').write_text(json.dumps(receipt,indent=2)+'\n')
 app.save_screenshot(str(evidence/'after-rightclick.svg'))
 await until(pilot,lambda:bool(app.screen.query(ContextMenuItem)))
 menu=next(item for item in app.screen.query(ContextMenuItem) if item.action==ForkAction.declared_name)
 assert await pilot.click(menu)
 await until(pilot,lambda:isinstance(app.screen,ForkDialog))
 app.save_screenshot(str(evidence/'fork-dialog.svg'))
 await pilot.press('escape')
 await until(pilot,lambda:not isinstance(app.screen,ForkDialog))
 assert not requests and len(comms.registry.all_threads())==2
 receipt.update(menu_opened=True,physical_fork_menu_click=True,dialog_opened=True,dialog_cancelled=True,provider_posts=len(requests),native_inputs_sent=0)
 (evidence/'menu-control.json').write_text(json.dumps(receipt,indent=2)+'\n')

asyncio.run(main(app_type=InstalledApp,acceptance=menu_control,provider_request_budget=0,headless=False,fixture_stage=Path(os.environ['INPUT_VISIBILITY_FIXTURE'])))
