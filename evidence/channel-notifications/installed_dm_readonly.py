"""Mount production DM views, read original assignments, send no messages."""
import asyncio
import json
import os
from pathlib import Path

HERE=Path(__file__).resolve().parent
TEMP=HERE.parents[1]/'.artifacts/dm-readonly'
for key,leaf in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_STATE_HOME','state')]:
    os.environ[key]=str(TEMP/leaf)
from agent_comms.comms import wire
from toad.app import ToadApp
from toad.widgets.comms_chat import CommsChatView
from toad.widgets.observed_thread_activity import ObservedThreadActivity

async def until(predicate):
    async with asyncio.timeout(20):
        while not predicate(): await asyncio.sleep(.05)

async def main():
    w=wire()
    names=('agent-comms-ux','pr95-selected-pi-summary-owner')
    owner=w.registry.require(names[0])
    app=ToadApp(project_dir=owner.worktree)
    report={'verified':False,'messages_sent':0,'views':{}}
    async with app.run_test(size=(150,48)) as pilot:
        original=app.current_mode
        for name in names:
            await app.open_comms_session(owner_mode=original,project_path=Path(owner.worktree),me='user',target=name,kind='dm')
            await until(lambda:bool(app.screen.query(CommsChatView)))
            dm=app.screen.query_one(CommsChatView)
            await until(lambda:dm.target==name and bool(dm.query(ObservedThreadActivity)))
            activity=dm.query_one(ObservedThreadActivity)
            await until(lambda:activity.presentation is not None and bool(activity.presentation.notifications))
            rendered=str(activity.render())
            assert '#comms' in rendered and 'Recent incoming messages:' in rendered
            report['views'][name]={'status':activity.presentation.summary,'rendered':rendered,'receipts':[
                {'seq':n.message.seq,'id':n.message.message_id,'target':n.message.target,'outcome':n.state}
                for n in activity.presentation.notifications]}
            await pilot.pause()
            app.save_screenshot(filename=f'installed-dm-{name}.svg',path=str(HERE))
        assert 'Checked — no response' in report['views'][names[1]]['rendered']
        assert app._exception is None
        report['verified']=True
    (HERE/'installed-dm-readonly.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

asyncio.run(main())
