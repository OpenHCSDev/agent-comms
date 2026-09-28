"""Observe existing user messages through installed DM UI; never submit input."""
import asyncio
import json
import os
import time
from pathlib import Path
HERE=Path(__file__).resolve().parent
TEMP=HERE.parents[1]/'.artifacts/watch-user'
for key,leaf in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_STATE_HOME','state')]:
 os.environ[key]=str(TEMP/leaf)
from agent_comms.comms import wire
from toad.app import ToadApp
from toad.widgets.comms_chat import CommsChatView
from toad.widgets.observed_thread_activity import ObservedThreadActivity

async def main():
 w=wire();owner=w.registry.require('agent-comms-ux')
 tracked=tuple(n.message for n in w.views.recent_notifications(owner.name) if n.message.seq in (77,79))
 assert {m.seq for m in tracked}=={77,79}
 app=ToadApp(project_dir=owner.worktree)
 started=time.monotonic();last=None
 report={'messages_sent':0,'observations':[],'verified':False}
 async with app.run_test(size=(150,48)) as pilot:
  await app.open_comms_session(owner_mode=app.current_mode,project_path=Path(owner.worktree),me='user',target=owner.name,kind='dm')
  async with asyncio.timeout(30):
   while not app.screen.query(ObservedThreadActivity):await asyncio.sleep(.05)
  view=app.screen.query_one(ObservedThreadActivity)
  (HERE/'watch-user.ready').write_text('mounted\n')
  for _ in range(2400):
   if view.presentation:
    deliveries=await asyncio.to_thread(w.views.message_notifications,tracked)
    outcomes={m.seq:next(n.state for n in deliveries[(m.seq,m.message_id)] if n.recipient==owner.name) for m in tracked}
    row={'status':view.presentation.summary,'busy':view.presentation.busy,'outcomes':outcomes,'receipts':[
     {'seq':n.message.seq,'id':n.message.message_id,'state':n.state} for n in view.presentation.notifications]}
    if row!=last:
     report['observations'].append({'seconds':round(time.monotonic()-started,2),**row});last=row
     print(json.dumps(report['observations'][-1]),flush=True)
     if row['busy']:app.save_screenshot(filename='installed-dm-channel-working.svg',path=str(HERE))
     (HERE/'watch-user-live.json').write_text(json.dumps(report,indent=2)+'\n')
    done=outcomes
    if done.get(79)=='Responded':
     report['verified']=True
     app.save_screenshot(filename='installed-dm-channel-completed.svg',path=str(HERE))
     break
   await asyncio.sleep(.25)
 report['elapsed_seconds']=round(time.monotonic()-started,2)
 (HERE/'watch-user-live.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report,indent=2),flush=True)
asyncio.run(main())
