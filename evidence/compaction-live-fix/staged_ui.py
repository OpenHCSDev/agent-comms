"""Mount installed Toad on real retained history through its archival owner."""
import asyncio
import faulthandler
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
ARTIFACTS = BASE / '.artifacts' / 'staged-ui-latest'
ROOT = ARTIFACTS / 'wire'


def prepare():
    ROOT.mkdir(parents=True)
    live = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
    shutil.copyfile(live / 'registry.json', ROOT / 'registry.json')
    for key, leaf in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[key] = str(ARTIFACTS / leaf)
    os.environ['AGENT_COMMS_ROOT'] = str(ROOT)
    from agent_comms.comms import wire
    from agent_comms.catalog_store import ChannelCatalog
    snapshot = wire(ROOT)
    snapshot.channels.catalog.replace(ChannelCatalog(live / ChannelCatalog.filename).read())
    for source in [live, *sorted((live / 'history').glob('source-*'))]:
        archive = Path(snapshot.bus.attach_history(source).root)
        ChannelCatalog(archive / ChannelCatalog.filename).replace(ChannelCatalog(source / ChannelCatalog.filename).read())
    print('Actual history archived; original roots unchanged', flush=True)


async def until(predicate):
    async with asyncio.timeout(25):
        while not predicate():
            await asyncio.sleep(.05)


async def main():
    from agent_comms.comms import wire
    from toad.app import ToadApp
    from toad.widgets.comms_chat import CommsChatView
    comms = wire()
    owner = comms.registry.require('agent-comms-ux')
    before = comms.bus.log.latest_sequence()
    app = ToadApp(project_dir=owner.worktree)
    report = {'messages_sent': 0, 'views': []}
    print('Mounting installed Toad', flush=True)
    async with app.run_test(size=(140, 48)) as pilot:
        print('Mounted', flush=True)
        original = app.current_mode
        for target, kind in [('#comms', 'channel'), ('agent-comms-ux', 'dm'), ('pr95-selected-pi-summary-owner', 'dm')]:
            print('Opening', target, flush=True)
            await app.open_comms_session(owner_mode=original, project_path=Path(owner.worktree), me='user', target=target, kind=kind)
            await until(lambda: bool(app.screen.query(CommsChatView)))
            view = app.screen.query_one(CommsChatView)
            await until(lambda: view.target == target and view._history_initialized)
            await pilot.pause()
            assert app._exception is None
            report['views'].append({'target': target, 'kind': kind, 'history_entries': len(view._history)})
        assert comms.bus.log.latest_sequence() == before
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    faulthandler.dump_traceback_later(45)
    prepare()
    asyncio.run(asyncio.wait_for(main(), 80))
    faulthandler.cancel_dump_traceback_later()
