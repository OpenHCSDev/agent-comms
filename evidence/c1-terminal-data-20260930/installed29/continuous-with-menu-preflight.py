import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,'/home/ts/wt/toad-input-delivery-visibility-20260930/tests')
from input_visibility_native_installed_pilot import InstalledApp,acceptance,review_frames
from l0a_native_installed_pilot import main,until
from saved_state_user_journey_pilot import reveal_thread_row
from toad.widgets.comms_sidebar import ChannelGroup
from toad.widgets.comms_menu import ContextMenu,ContextMenuItem
from toad.thread_actions import ForkAction

async def continuous(app,pilot,agent,comms,entered,release,hold_next,requests):
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

asyncio.run(main(app_type=InstalledApp,acceptance=continuous,expected_response_disconnects=frozenset({3,5}),provider_request_budget=5,headless=False,fixture_stage=Path(os.environ['INPUT_VISIBILITY_FIXTURE'])))
evidence=Path(os.environ['L0A_EVIDENCE'])
(evidence/'input-visibility-review.json').write_text(json.dumps(review_frames(evidence/'input-frames.jsonl'),indent=2)+'\n')
