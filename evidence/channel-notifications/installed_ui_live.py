"""Production Toad mounted on actual route; send once through real channel composer."""
import asyncio
import faulthandler
import json
import os
import sys
import time
from pathlib import Path

EVIDENCE=Path(__file__).resolve().parent
ARTIFACTS=EVIDENCE.parents[1]/'.artifacts/channel-ui'
ARTIFACTS.mkdir(parents=True,exist_ok=True)
for key,leaf in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_STATE_HOME','state')]:
    os.environ[key]=str(ARTIFACTS/leaf)
# Keep route discovery in the actual user state directory before isolating UI prefs.
from agent_comms.comms import wire
from agent_comms.active_route import read_active_route
route_path=Path('/home/ts/.local/state/agent-comms/active-route.json')
route=json.loads(route_path.read_text())
for key in ('AGENT_COMMS_ROOT','AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID','AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE','PYTHONPATH'):
    os.environ.pop(key,None)
from toad.app import ToadApp
from toad import messages
from toad.widgets.comms_chat import CommsChatView
from toad.widgets.message_notifications import MessageNotifications
from toad.widgets.observed_thread_activity import ObservedThreadActivity

async def until(predicate,timeout=30):
    async with asyncio.timeout(timeout):
        while not predicate(): await asyncio.sleep(.05)

async def main():
    w=wire()
    owner=w.registry.require('agent-comms-ux')
    assert owner.active_turn is None
    app=ToadApp(project_dir=owner.worktree)
    report={'verified':False,'observations':[],'provider_sends':'one fresh channel message; existing subscribers decide','replayed_inputs':0}
    started=time.monotonic()
    try:
        async with app.run_test(size=(140,48)) as pilot:
            original_mode=app.current_mode
            await app.open_comms_session(owner_mode=original_mode,project_path=Path(owner.worktree),me='user',target='#comms',kind='channel')
            await until(lambda: len(app.screen.query(CommsChatView))>0)
            chat=app.screen.query_one(CommsChatView)
            await until(lambda: chat._history_initialized and not chat._refresh_lock.locked())
            from uuid import uuid4
            probe_id = uuid4().hex[:8]
            previous_ids = {m.message_id for m, _ in chat._history}
            body=f'Independent UI check {probe_id} for the Comms UX owner: reply TOAD_FEEDBACK_SECOND_CHECK_OK in this channel. No tools or file edits. PR95 owner: this is outside your task; ignore it.'
            print('SENDING FRESH UI CHECK',flush=True)
            await chat.submit_input(messages.UserInputSubmitted(body))
            print('SEND AND PAINT RETURNED',flush=True)
            await until(lambda:any(m.body==body and m.message_id not in previous_ids for m,_ in chat._history))
            message=next(m for m,_ in chat._history if m.body==body and m.message_id not in previous_ids)
            report['sequence']=message.seq
            report['message_id']=message.message_id
            report['probe_id']=probe_id
            channel_mode=app.current_mode
            dm_checked=False
            last=None
            for _ in range(400):
                card=next((v for m,v in chat._history if m.seq==message.seq and m.message_id==message.message_id),None)
                if card is not None:
                    feedback=card.query_one(MessageNotifications)
                    summary=str(feedback.title)
                    statuses={v.thread.name:v.presentation.summary for v in await asyncio.to_thread(w.views.thread_views) if v.thread.name in ('agent-comms-ux','pr95-selected-pi-summary-owner')}
                    observation={'summary':summary,'thread_status':statuses}
                    if observation!=last:
                        report['observations'].append({'seconds':round(time.monotonic()-started,2),**observation})
                        print(report['observations'][-1],flush=True);last=observation
                    if not dm_checked and 'Checking #comms message' in statuses.get(owner.name,''):
                        await app.open_comms_session(owner_mode=original_mode,project_path=Path(owner.worktree),me='user',target=owner.name,kind='dm')
                        await until(lambda:app.screen.query_one(CommsChatView).kind=='dm')
                        dm=app.screen.query_one(CommsChatView)
                        await until(lambda:bool(dm.query(ObservedThreadActivity)))
                        observed=dm.query_one(ObservedThreadActivity)
                        await until(lambda:observed.presentation is not None and observed.presentation.busy,timeout=10)
                        report['dm_status_during_channel_turn']=observed.presentation.summary
                        app.save_screenshot(filename='installed-dm-active.svg',path=str(EVIDENCE))
                        await app.switch_mode(channel_mode)
                        dm_checked=True
                    if 'Responded' in summary and 'Checked — no response' in summary:
                        feedback.collapsed=False
                        await pilot.pause()
                        report['expanded_details']=str(feedback.details.render())
                        app.save_screenshot(filename='installed-channel-feedback.svg',path=str(EVIDENCE))
                        break
                await asyncio.sleep(.15)
            observations=json.dumps(report['observations'],ensure_ascii=False)
            assert 'Checking relevance' in observations and 'Responding' in observations and 'Responded' in observations
            assert 'Checking #comms message' in observations and 'Responding in #comms' in observations
            # Open real DM view afterward; the worker additionally checks active
            # DM/native Ready suppression with mounted local UI transitions.
            await app.open_comms_session(owner_mode=original_mode,project_path=Path(owner.worktree),me='user',target=owner.name,kind='dm')
            await until(lambda:app.screen.query_one(CommsChatView).kind=='dm')
            dm=app.screen.query_one(CommsChatView)
            await until(lambda:bool(dm.query(ObservedThreadActivity)))
            observed=dm.query_one(ObservedThreadActivity)
            await until(lambda:observed.presentation is not None)
            report['dm_status_after_completion']=observed.presentation.summary
            assert app._exception is None
            report['verified']=True
    finally:
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        (EVIDENCE/'installed-ui-live.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2),flush=True)

if __name__=='__main__':
    faulthandler.dump_traceback_later(15, repeat=True)
    asyncio.run(main())
