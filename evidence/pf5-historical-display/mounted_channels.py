"""Mount the copied original channel pages through installed Toad; never send."""
import asyncio
import faulthandler
import sys
import json
import os
from pathlib import Path

OWN = Path(__file__).resolve().parents[2]
ROOT = OWN / '.artifacts/original-history'
PROJECT = OWN / '.artifacts/ui-project'
PROJECT.mkdir(exist_ok=True)
for key, leaf in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
    os.environ[key] = str(OWN / '.artifacts/ui' / sys.argv[1] / leaf)
os.environ['AGENT_COMMS_ROOT'] = str(ROOT)

from agent_comms.comms import wire
from agent_comms.historical_views import HistoricalMessage
from toad.app import ToadApp
from toad.widgets.comms_chat import CommsChatView

async def until(predicate):
    async with asyncio.timeout(30):
        while not predicate():
            await asyncio.sleep(.05)

async def main():
    c = wire()
    sequence = c.bus.log.latest_sequence()
    app = ToadApp(project_dir=str(PROJECT))
    report = {}
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.sleep(.05)
        owner_mode = app.current_mode
        for target in ('#comms', '#nra'):
            await app.open_comms_session(owner_mode=owner_mode, project_path=PROJECT, me='user', target=target, kind='channel')
            await until(lambda: any(view.target == target for view in app.screen.query(CommsChatView)))
            chat = next(view for view in app.screen.query(CommsChatView) if view.target == target)
            await until(lambda: chat._history_initialized and not chat._refresh_lock.locked() and not chat._edge_load_scheduled)
            await pilot.pause()
            for _ in range(8):
                old = [message for message, _widget in chat._history if isinstance(message, HistoricalMessage)]
                if old:
                    break
                cursor = chat._history[0][0].view_cursor
                chat.window.release_anchor()
                chat.window.scroll_to(y=0, animate=False, immediate=True)
                chat._on_window_scroll()
                await until(lambda: chat._history[0][0].view_cursor != cursor or not chat._has_older)
            assert old, target
            report[target] = {'mounted_original_rows': len(old), 'first_sequence': old[0].seq}
            print('mounted', target, report[target], flush=True)
        assert app._exception is None
    assert c.bus.log.latest_sequence() == sequence
    print(json.dumps({'channels': report, 'copied_bus_sequence_unchanged': True}), flush=True)
    await asyncio.get_running_loop().shutdown_default_executor()

faulthandler.dump_traceback_later(25, repeat=True)
asyncio.run(main())
faulthandler.cancel_dump_traceback_later()
