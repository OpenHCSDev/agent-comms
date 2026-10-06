"""Original continuous installed runner with retained SDK source and ANSI proof."""
import asyncio,importlib.util,json,os,sys,time
from pathlib import Path
sys.path.insert(0,'/home/ts/wt/toad-native-fixture-cleanup-20260930/tests')
from l0a_native_installed_pilot import main,until
from agent_comms.field_codec import FieldCodec
from toad.widgets.comms_fork_dialog import ForkDialog
from toad.widgets.session_details import SessionDetails
from toad.widgets.transcript_history import TranscriptHistory
import first_fork_native_installed_pilot as fork_driver

def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module
base=load('accepted_native_observer','/home/ts/wt/comms-c1-terminal-data-20260930/evidence/c1-terminal-data-20260930/installed33/continuous-with-menu-preflight-guarded.py')
retained=load('retained_prepare','/home/ts/wt/g458j/tools/retained-prepare.py')
evidence=Path(os.environ['L0A_EVIDENCE'])

class InstalledApp(base.InstalledApp):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**kwargs)
  self.observed_modal=None
  self.latest_original_frame=None

 def observe_input_paint(self,frame):
  super().observe_input_paint(frame)
  text='\n'.join(self.measurement_terminal.display)
  if isinstance(self.screen,ForkDialog) and all(marker in text for marker in ('Fork from @beta','Name','Tags','Task / goal / prompt')):
   self.observed_modal=(self.screen,frame.observed_ns)
   if not (evidence/'fork-controls-painted.json').exists():
    (evidence/'fork-controls-painted.json').write_text(json.dumps({'frame':FieldCodec.encode(frame),'screen':type(self.screen).__name__,'actual_original_incremental_ansi':True},indent=2)+'\n')
    (evidence/'fork-controls-painted.txt').write_text(text+'\n')
  self.latest_original_frame=frame

original_wait_dialog=fork_driver.wait_fork_dialog
async def wait_painted_fork(app,pilot,**kwargs):
 dialog=await original_wait_dialog(app,pilot,**kwargs)
 await until(pilot,lambda:app.observed_modal is not None and app.observed_modal[0] is dialog)
 return dialog
fork_driver.wait_fork_dialog=wait_painted_fork

async def acceptance(app,pilot,agent,comms,entered,release,hold_next,requests):
 try:
  view=app.selected_session.conversation
  def original_saved_paint():
   if app.measurement_terminal is None:return False
   body=app.measurement_terminal.region_text(lambda frame:frame.chat_region)
   return len([c for c in body if c.isalnum()])>=20 and bool(view.contents.query(TranscriptHistory))
  await until(pilot,original_saved_paint,30)
  (evidence/'retained-saved-startup.txt').write_text('\n'.join(app.measurement_terminal.display)+'\n')
  (evidence/'retained-saved-startup.json').write_text(json.dumps({'requests':len(requests),'original_ansi_readable':True,
   'source':retained.verify_source(),'histories':[{'resource_id':id(h),'through':FieldCodec.encode(h.through),'pages':len(h.pages)} for h in view.contents.query(TranscriptHistory)]},indent=2)+'\n')
  assert not requests
  print('RETAINED_SAVED_HISTORY_PHYSICALLY_READABLE',flush=True)
  # Same original source entry, no provider POST: painted and eligible Fork
  # dialog Escape must dismiss without creating a child before seed admission.
  from saved_state_user_journey_pilot import reveal_thread_row
  from toad.widgets.comms_menu import ContextMenu,ContextMenuItem
  from toad.thread_actions import ForkAction
  from textual.widgets import Input,TextArea
  original_screen=app.screen
  before_names=tuple(comms.registry.all_threads())
  row=await reveal_thread_row(app,pilot,'beta','#team')
  assert await pilot.click(row,button=3)
  await until(pilot,lambda:isinstance(app.screen,ContextMenu) and bool(app.screen.query(ContextMenuItem)))
  action=next(item for item in app.screen.query(ContextMenuItem) if item.action==ForkAction.declared_name)
  assert await pilot.click(action)
  dialog=await wait_painted_fork(app,pilot)
  assert dialog.query_one('#fork-tags',Input).value==', '.join(sorted(comms.registry.require('beta').tags))
  assert dialog.query_one('#fork-task',TextArea).text==''
  assert dialog.query_one('#fork-name',Input).value==''
  eligible={'observed_ns':app.observed_modal[1],'focused_name':app.focused.id,
   'inherited_tags':dialog.query_one('#fork-tags',Input).value,
   'task':dialog.query_one('#fork-task',TextArea).text,'name':dialog.query_one('#fork-name',Input).value,
   'provider_posts':len(requests),'threads_before':before_names}
  (evidence/'fork-escape-preseed.json').write_text(json.dumps(eligible,indent=2)+'\n')
  await pilot.press('escape')
  await until(pilot,lambda:app.screen is original_screen)
  assert tuple(comms.registry.all_threads())==before_names and not requests
  eligible.update(escaped=True,child_created=False)
  (evidence/'fork-escape-preseed.json').write_text(json.dumps(eligible,indent=2)+'\n')
  print('PAINTED_FOCUSED_FORK_ESCAPE_WITHOUT_CHILD_OR_POST',flush=True)
  await base.continuous(app,pilot,agent,comms,entered,release,hold_next,requests)
  # Observe canonical settlement and an actual idle caption emitted naturally;
  # no forced compositor render, status publication or new input stimulates it.
  def stationary_ready():
   current=app.selected_session.conversation
   details=current.query_one(SessionDetails);source=details.activity.presentation
   text='\n'.join(app.measurement_terminal.display)
   owner=comms.registry.require(current.agent.session_id)
   return (not owner.executing and not current.turns.owner.busy
    and current.turns.owner.state.finished_turn_id==owner.last_finished_turn_id
    and not current.submissions.active and not current.submissions.queue_projection.items
    and source is not None and not source.busy and source.summary=='Ready'
    and 'Ready' in str(details.title)
    and any('Session details' in line and 'Ready' in line for line in app.measurement_terminal.display)
    and not any(word in text for word in ('Cancelling…','Responding in','Thinking…')))
  await until(pilot,stationary_ready,20)
  current=app.selected_session.conversation
  details=current.query_one(SessionDetails);source=details.activity.presentation
  (evidence/'stationary-ready.txt').write_text('\n'.join(app.measurement_terminal.display)+'\n')
  (evidence/'stationary-ready.json').write_text(json.dumps({
   'observed_ns':app.latest_original_frame.observed_ns,'original_frame':FieldCodec.encode(app.latest_original_frame),
   'original_turn_state':FieldCodec.encode(current.turns.owner.state),
   'canonical_registry_owner':FieldCodec.encode(comms.registry.require(current.agent.session_id)),
   'presentation_summary':source.summary,'presentation_busy':source.busy,
   'presentation_binding':FieldCodec.encode(source.binding),'session_details_title':str(details.title),
   'after_original_fifth_cancel_completion':True,'original_emitted_ansi_no_forced_repaint':True},indent=2)+'\n')
  await pilot.pause(.5)
  view=app.selected_session.conversation
  assert not view.turns.owner.busy and not view.submissions.active
  assert len(requests)==5 and not (evidence/'duplicate-at-emission.json').exists()
  (evidence/'retained-final-source.json').write_text(json.dumps(retained.verify_source(),indent=2)+'\n')
  print('STATIONARY_CANCEL_READY_PHYSICALLY_PAINTED',flush=True)
 except BaseException:
  import traceback
  (evidence/'retained-acceptance-failure.txt').write_text(traceback.format_exc())
  if app.measurement_terminal is not None:(evidence/'retained-failure-terminal.txt').write_text('\n'.join(app.measurement_terminal.display)+'\n')
  raise

if __name__=='__main__':
 asyncio.run(main(app_type=InstalledApp,prepare_state=retained.prepare,acceptance=acceptance,
  expected_response_disconnects=frozenset({3,5}),provider_request_budget=5,headless=False,
  fixture_stage=Path(os.environ['INPUT_VISIBILITY_FIXTURE'])))
 (evidence/'input-visibility-review.json').write_text(json.dumps(base.review_frames(evidence/'input-frames.jsonl'),indent=2)+'\n')
