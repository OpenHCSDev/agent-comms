import asyncio,json,os
from pathlib import Path
state=Path('/home/ts/.local/state/agent-comms');out=state/'r1-pi-history-ui';out.mkdir(exist_ok=True)
os.environ['XDG_CONFIG_HOME']=str(out/'config');os.environ['XDG_DATA_HOME']=str(out/'data')
from agent_comms.comms import wire
from agent_comms.historical_views import HistoricalMessage
from toad.app import ToadApp
from toad.widgets.comms_chat import CommsChatView
from toad.screens.historical_sessions import HistoricalSessions
from toad.widgets.transcript_history import TranscriptHistory
from textual.widgets import Select

async def until(pilot,predicate):
 async with asyncio.timeout(20):
  while not predicate():await pilot.pause(.05)

async def main():
 c=wire();seq=c.bus.log.latest_sequence();report={'root':str(c.root),'channels':{}}
 app=ToadApp(project_dir='/home/ts/.agent-comms')
 async with app.run_test(size=(120,40)) as pilot:
  await pilot.pause()
  owner_mode=app.current_mode
  for target in ('#comms','#nra'):
   await app.open_comms_session(owner_mode=owner_mode,project_path=Path('/home/ts/.agent-comms'),me='user',target=target,kind='channel')
   await until(pilot,lambda:any(v.target==target for v in app.screen.query(CommsChatView)))
   chat=next(v for v in app.screen.query(CommsChatView) if v.target==target)
   await until(pilot,lambda:chat._history_initialized)
   for _ in range(8):
    old=[m for m,_ in chat._history if isinstance(m,HistoricalMessage)]
    if old:break
    cursor=chat._history[0][0].view_cursor
    chat.window.scroll_home(animate=False,immediate=True)
    await until(pilot,lambda:chat._history[0][0].view_cursor!=cursor or not chat._has_older)
   assert old,target
   report['channels'][target]={'mounted_historical_rows':len(old),'original_source':old[0].source.original_root,'first_source_sequence':old[0].seq}
   app.save_screenshot(filename=target[1:]+'.svg',path=str(out))
  await app.screen.action_historical_sessions()
  await until(pilot,lambda:isinstance(app.screen,HistoricalSessions))
  modal=app.screen
  await until(pilot,lambda:bool(modal.query("#saved-identity")))
  index=next(i for i,v in enumerate(modal.threads) if v.thread.name=='agent-comms-ux' and v.thread.session_file)
  modal.query_one('#saved-identity',Select).value=index
  await until(pilot,lambda:bool(modal.query(TranscriptHistory)))
  transcript=modal.query_one(TranscriptHistory)
  await until(pilot,lambda:bool(transcript.pages))
  report['saved_session']={'thread':modal.threads[index].thread.name,'path':modal.threads[index].thread.session_file,'pages_mounted':len(transcript.pages),'visible_identity_count':len(modal.threads)}
  app.save_screenshot(filename='saved-session.svg',path=str(out))
  assert app._exception is None
 report['live_sequence_unchanged']=c.bus.log.latest_sequence()==seq
 assert report['live_sequence_unchanged']
 (out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2),flush=True)
 await asyncio.get_running_loop().shutdown_default_executor()
if __name__ == '__main__':
    asyncio.run(main())
