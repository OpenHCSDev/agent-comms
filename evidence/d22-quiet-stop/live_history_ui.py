"""Installed Toad against the converted live history, with no input submission."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

async def until(predicate):
    async with asyncio.timeout(25):
        while not predicate():
            await asyncio.sleep(.05)

async def main():
    from agent_comms.comms import wire
    from toad.app import ToadApp
    from toad.widgets.comms_chat import CommsChatView
    comms=wire()
    owner=comms.registry.require('agent-comms-ux')
    before=comms.bus.log.latest_sequence()
    app=ToadApp(project_dir=owner.worktree)
    results=[]
    async with app.run_test(size=(140,48)) as pilot:
        original=app.current_mode
        for target,kind in (('#comms','channel'),('agent-comms-ux','dm'),('pr95-selected-pi-summary-owner','dm')):
            await app.open_comms_session(owner_mode=original,project_path=Path(owner.worktree),me='user',target=target,kind=kind)
            await until(lambda: bool(app.screen.query(CommsChatView)))
            view=app.screen.query_one(CommsChatView)
            await until(lambda: view.target==target and view._history_initialized)
            await pilot.pause()
            assert app._exception is None
            results.append({'target':target,'history_entries':len(view._history)})
        assert comms.bus.log.latest_sequence()==before
    print(json.dumps({'views':results,'messages_sent':0,'bus_sequence':before}),flush=True)

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='live-history-ui-',dir=Path(__file__).resolve().parents[2]/'.artifacts') as folder:
        for key,leaf in (('XDG_CONFIG_HOME','config'),('XDG_STATE_HOME','state'),('XDG_DATA_HOME','data')):
            os.environ[key]=str(Path(folder)/leaf)
        asyncio.run(main())
    print('PASS: mounted converted live history and interpreter shutdown',flush=True)
