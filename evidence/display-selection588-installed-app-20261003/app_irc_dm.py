"""Affected installed App path, adapted from channel_views_pilot/human_mark_read_pilot.

Real Comms messages and original App/history/painted-ACK owners; no ACP substitute.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent
RUN = OUT / 'app05'
from agent_comms.comms import wire
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread
from toad.app import ToadApp
from toad.navigation_target import NavigationContext, DirectTarget, channel_target
from toad.widgets.comms_chat import CommsChatView
from toad.session_admission import HistorySessionAdmission
from toad.navigation_preparation import CommsNavigationRequest
from toad.conversation_kind import DmConversation, ChannelConversation

async def until(pilot, predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await pilot.pause(.02)

async def refresh(chat):
    worker = await chat._refresh()
    if worker is not None:
        await worker.wait()

async def frame(app, pilot, label, chat):
    await pilot.pause()
    path = RUN / (label + '.svg')
    app.save_screenshot(str(path))
    subprocess.run(['rsvg-convert', '-o', str(path.with_suffix('.png')), str(path)], check=True)
    records.append({'label': label, 'at': time.time(), 'selectedMode': app.selected_mode,
                    'svgSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'rows': [{'id': m.message_id, 'seq': m.seq, 'body': m.body}
                             for m, _ in chat.message_history.rows]})
    (RUN / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    print('INSPECT', label, str(path.with_suffix('.png')), flush=True)
    await pilot.pause(8)

async def main():
    global COMMS, root_id, native, viewer, records, receipt
    RUN.mkdir(mode=0o700)
    ORIGINAL = OUT / 'app02'
    ROOT = ORIGINAL / 'wire'
    PROJECT = ORIGINAL / 'project'
    os.environ.update(AGENT_COMMS_ROOT=str(ROOT), XDG_CONFIG_HOME=str(RUN / 'config'),
                      XDG_STATE_HOME=str(RUN / 'state'), XDG_DATA_HOME=str(RUN / 'data'),
                      TOAD_TEST_ATTEMPT='Sch588-app01')
    
    COMMS = wire(ROOT)
    prior = json.loads((ORIGINAL / 'receipt.json').read_text())
    root_id = prior['rootId']
    native = json.loads((OUT / 'ownership.json').read_text())['native']
    os.environ.update(AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
                      AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=native)
    viewer = COMMS.messaging.user_identity(str(PROJECT)).name
    records = []
    receipt = {'state': 'running', 'scope': 'Installed standalone saved-bus App/IRC/DM and canonical messaging/painted ACK; no native-tab/ACP navigation claim',
               'publicEffects': 0, 'providerCalls': 0, 'mockProtocols': 0, 'continuationOf': str(ORIGINAL / 'receipt.json'), 'newMessages': 0, 'root': str(ROOT),
               'rootId': root_id, 'startedAt': time.time(), 'frames': records}
    
    prepared = CommsNavigationRequest(str(ROOT), 'savedbus', viewer, 'peer', DmConversation, None).read()
    app = ToadApp(project_dir=str(PROJECT), mode='dm588')
    dm_admission = HistorySessionAdmission('dm588', prepared.key, DmConversation, PROJECT, None)
    app.workspace_sessions.register(dm_admission.mode, dm_admission)
    app.tab_order.open(dm_admission.mode)
    try:
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await dm_admission.ready(app.session_navigation)
            chat = app.workspace_sessions.require(app.selected_mode).query_one(CommsChatView)
            dm = next(m for m in COMMS.views.dm_display_page('peer', worktree=str(PROJECT)).messages
                      if m.body == '588 DM actual painted reply')
            await until(pilot, lambda: any(m.message_id == dm.message_id for m, _ in chat.message_history.rows))
            await frame(app, pilot, 'dm-painted', chat)
            await until(pilot, lambda: COMMS.bus.pending_count(viewer, 'peer') == 0)
            assert COMMS.bus.pending_count('recipient', 'peer') == 1
            receipt['dmPaintedId'] = dm.message_id
            prepared = CommsNavigationRequest(str(ROOT), 'savedbus', viewer, '#app588', ChannelConversation, None).read()
            admission = HistorySessionAdmission('channel588', prepared.key, ChannelConversation, PROJECT, None)
            await app.session_navigation.admit(admission)
            await admission.ready(app.session_navigation)
            chat = app.workspace_sessions.require(app.selected_mode).query_one(CommsChatView)
            published = [tuple(row) for row in prior['channelPublished']]
            await until(pilot, lambda: len([m for m, _ in chat.message_history.rows if m.body.startswith('588 channel')]) == 20)
            assert [(m.seq, m.message_id) for m, _ in chat.message_history.rows if m.body.startswith('588 channel')] == published
            await frame(app, pilot, 'channel-saved', chat)
            assert COMMS.views.viewer_snapshot(str(PROJECT)).channel_unread.get('#app588', 0) == 0
            assert COMMS.bus.pending_count('recipient', '#app588') == 20
            await app.select_session(dm_admission.mode)
            chat = app.workspace_sessions.require(app.selected_mode).query_one(CommsChatView)
            assert any(m.message_id == dm.message_id for m, _ in chat.message_history.rows)
            await frame(app, pilot, 'dm-return', chat)
            assert app._exception is None
        await asyncio.get_running_loop().shutdown_default_executor()
        receipt.update(state='PASS', finishedAt=time.time(), appException=None,
                       channelViewerUnread=0, dmViewerUnread=0,
                       executorChannelPending=20, executorDmPending=1)
    except BaseException as error:
        receipt.update(state='FAIL', finishedAt=time.time(), error=repr(error))
        raise
    finally:
        receipt['privateWorkerPids'] = [t.pid for t in COMMS.registry.all_threads().values()
                                        if t.pid > 0 and t.pid != os.getpid()]
        (RUN / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)

if __name__ == '__main__':
    asyncio.run(main())
