"""Normal installed fresh-fork wire view against copied actual saved history.

No Node/provider call, source/store reset, patched transport or live write.
The supplied current checkpoint schema is checked read-only before copying.
Only destination-owned registry/checkpoint inode seals are rebuilt by their
canonical owners; public/private publication bytes remain unchanged.
"""
import asyncio
from contextlib import closing
from dataclasses import replace
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, os.environ['TOAD_TEST_HELPERS'])
from runtime_fixture import ToadApp
from sidebar_retirement_pilot import until, viewport_text
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.private_bus_checkpoint import _connect, _saved, install_private_bus_checkpoint
from agent_comms.wire_log import WireLog
from agent_comms.store_files import file_revision
from agent_comms.threads import Thread
from toad.navigation_target import DirectTarget, channel_target
from toad.navigation_preparation import ThreadNavigationRequest
from toad.widgets.comms_chat import CommsChatView


class InstalledApp(ToadApp):
    CSS_PATH = files('toad').joinpath('toad.tcss')


def copy_actual_history(destination, live):
    result=[]
    originals={}
    for index, raw in enumerate(json.loads((live/'history_sources.json').read_text())):
        source=FieldCodec.decode(HistorySource, raw)
        original=Path(source.root)
        with closing(_connect(original/'private_bus_checkpoint.sqlite3',readonly=True)) as db:
            _saved(db)
        copied=destination/'history'/f'copied-{index}'
        shutil.copytree(original,copied)
        (copied/'.registry-owner-guard').unlink(missing_ok=True)
        originals[copied]=(original,(original/'bus.jsonl').read_bytes(),
                           (original/'private_bus_checkpoint.sqlite3').read_bytes())
        guard=PrivateRegistryGuard(copied/'registry.json',source.wire_root_id)
        guard.create_pending()
        guard.commit_initial()
        log=WireLog(copied/'bus.jsonl')
        marker=log.read_metadata_unlocked(required=True)
        marker.checkpoint_version=None
        marker.checkpoint_seal=None
        (copied/'private_bus_checkpoint.sqlite3').unlink()
        log.write_metadata_unlocked(marker)
        install_private_bus_checkpoint(log)
        result.append(replace(source,root=str(copied),snapshot_bus_revision=file_revision(copied/'bus.jsonl'),
                              snapshot_registry_revision=file_revision(copied/'registry.json')))
    (destination/'history_sources.json').write_text(json.dumps([FieldCodec.encode(s) for s in result]))
    return originals


async def main():
    live=Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT'])
    with TemporaryDirectory(prefix='actual-archive-',dir=os.environ['TMPDIR']) as directory:
        root=Path(directory)
        os.environ.update(AGENT_COMMS_ROOT=str(root/'wire'),XDG_CONFIG_HOME=str(root/'config'),
                          XDG_STATE_HOME=str(root/'state'),XDG_DATA_HOME=str(root/'data'))
        comms=wire(root/'wire')
        comms.messaging.initialize_private_initial_protocol()
        for name, parent in [('openhcs-architecture-memory',None),('openhcs-pr159-viewer-bind-owner','openhcs-architecture-memory')]:
            comms.threads.register(Thread(name,frozenset({'openhcs','comms'}),str(root),parent=parent,
                                          process_identity=None))
        originals=copy_actual_history(comms.root,live)
        navigation=ThreadNavigationRequest(str(comms.root),'openhcs-pr159-viewer-bind-owner',root,()).read()
        assert navigation.thread.name=='openhcs-pr159-viewer-bind-owner'
        print('CANONICAL_FRESH_FORK_NAVIGATION_METADATA_LOADED',flush=True)
        me=comms.messaging.user_identity(str(root)).name
        live_bus_before=(comms.root/'bus.jsonl').read_bytes()
        app=InstalledApp(project_dir=str(root))
        try:
            await exercise_ui(app,root,me)
        finally:
            await asyncio.get_running_loop().shutdown_default_executor()
        assert (comms.root/'bus.jsonl').read_bytes()==live_bus_before
        assert not comms.bus.incoming_page('openhcs-pr159-viewer-bind-owner',after=0).messages
        for copied,(original,bus,checkpoint) in originals.items():
            assert (copied/'bus.jsonl').read_bytes()==bus
            assert (original/'bus.jsonl').read_bytes()==bus
            assert (original/'private_bus_checkpoint.sqlite3').read_bytes()==checkpoint
        print('CURRENT_PUBLIC_AND_PRIVATE_HISTORY_BYTES_PRESERVED',flush=True)
        print('NO_LIVE_BUS_WRITES_OR_HISTORICAL_WAKE_AUTHORITY',flush=True)


async def exercise_ui(app,root,me):
    async with app.run_test(size=(139,35)) as pilot:
        await pilot.pause(.05)
        owner=app.selected_mode
        await app.open_comms_session(owner_mode=owner,project_path=root,me=me,
                                    target=DirectTarget('openhcs-pr159-viewer-bind-owner'))
        chat=app.selected_session.query_one(CommsChatView)
        await until(pilot,lambda: chat._history_initialized or 'Wire error:' in chat.status)
        assert 'Wire error:' not in chat.status,chat.status
        assert chat._history_initialized and chat.prompt.prompt_text_area.has_focus
        print('INSTALLED_FRESH_FORK_WIRE_LOAD_NO_SCHEMA_ERROR',flush=True)
        await app.open_comms_session(owner_mode=owner,project_path=root,me=me,
                                    target=channel_target('#comms'))
        historical=app.selected_session.query_one(CommsChatView)
        await until(pilot,lambda: bool(historical._history) or 'Wire error:' in historical.status)
        assert 'Wire error:' not in historical.status,historical.status
        historical.window.scroll_end(animate=False,immediate=True)
        message=historical._history[-1][0]
        words=[word[:12] for word in message.body.split() if len(word)>=8]
        assert words,'The actual source message must have a visible text witness'
        await until(pilot,lambda: any(word in viewport_text(historical) for word in words))
        assert not message.starts_turn
        assert app._exception is None
        print('ACTUAL_ARCHIVED_PUBLIC_MESSAGE_PAINTED_NO_DELIVERY_AUTHORITY',flush=True)

if __name__=='__main__':
    asyncio.run(main())
