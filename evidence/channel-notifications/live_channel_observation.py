"""Observe real channel dispatch and existing owner decisions, without mocked backends."""
import json
import time
from pathlib import Path
from dataclasses import asdict
from agent_comms.comms import wire

w=wire()
for name in ('agent-comms-ux','pr95-selected-pi-summary-owner'):
    assert w.registry.require(name).active_turn is None
message=w.messaging.send_user_message('#comms',
    'Live channel feedback verification for the Comms UX owner. Please reply CHANNEL_FEEDBACK_OK in this channel. Do not use tools, modify files, or resume old tasks. This concerns the Comms UX owner only; the PR95 owner should ignore it.',
    worktree='/home/ts/.agent-comms')
report={'sequence':message.seq,'message_id':message.message_id,'observations':[], 'verified':False}
start=time.monotonic();previous=None
try:
    while time.monotonic()-start < 120:
        rows=w.views.message_notifications([message])[(message.seq,message.message_id)]
        selected=[asdict(row) for row in rows if row.recipient in ('agent-comms-ux','pr95-selected-pi-summary-owner')]
        if selected!=previous:
            report['observations'].append({'seconds':round(time.monotonic()-start,3),'recipients':selected})
            print(report['observations'][-1],flush=True);previous=selected
        if any(r['recipient']=='agent-comms-ux' and r['state']=='Responded' for r in selected) and any(r['recipient']=='pr95-selected-pi-summary-owner' and r['state']=='Checked — no response' for r in selected):
            break
        time.sleep(.2)
    states={r['state'] for o in report['observations'] for r in o['recipients']}
    report['verified']={'Checking','Responding','Responded','Checked — no response'} <= states
    assert report['verified'],report
finally:
    Path(__file__).with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
